# -*- coding: utf-8 -*-
from openerp import osv, models, fields, api, _
from openerp.osv import fields as old_fields
from openerp.exceptions import except_orm, Warning
import openerp.addons.decimal_precision as dp
# from inspect import currentframe, getframeinfo
# estas 2 lineas son para imprimir el numero de linea del script
# (solo para debug)
# frameinfo = getframeinfo(currentframe())
# print(frameinfo.filename, frameinfo.lineno)
import logging
_logger = logging.getLogger(__name__)

class AccountInvoiceLine(models.Model):
    _inherit = 'account.invoice.line'

    @api.one
    @api.depends('price_unit', 'discount', 'invoice_line_tax_ids', 'quantity',
        'product_id', 'invoice_id.partner_id', 'invoice_id.currency_id', 'invoice_id.company_id')
    def _compute_price(self):
        super(AccountInvoiceLine,self)._compute_price()
        currency = self.invoice_id and self.invoice_id.currency_id or None
        price = self.price_unit * (1 - (self.discount or 0.0) / 100.0)
        taxes = False
        if self.invoice_line_tax_ids.price_include:
            taxes = self.invoice_line_tax_ids.compute_all(price, currency, self.quantity, product=self.product_id, partner=self.invoice_id.partner_id)
        self.price_tax_included = taxes['total_included'] if (taxes and taxes['total_included'] > self.quantity * price) else self.quantity * price

    price_tax_included = fields.Monetary(string='Amount', readonly=True, compute='_compute_price')

class AccountInvoiceTax(models.Model):
    _inherit = "account.invoice.tax"

    def _compute_base_amount(self):
        for tax in self:
            if tax.tax_id.price_include:
                base = 0.0
                for line in tax.invoice_id.invoice_line_ids:
                    if tax.tax_id in line.invoice_line_tax_ids:
                        neto = round(line.price_tax_included / (1 + tax.tax_id.amount / 100))
                        iva_round =  round(neto * ( tax.tax_id.amount / 100))
                        if round(neto+iva_round) != round(line.price_tax_included):
                            neto = int(line.price_tax_included / (1 + tax.tax_id.amount / 100))
                        base += neto
                        base += sum((line.invoice_line_tax_ids.filtered(lambda t: t.include_base_amount) - tax.tax_id).mapped('amount'))
                tax.base = tax.invoice_id.currency_id.round(base)# se redondea global
            else:
                super(AccountInvoiceTax,tax)._compute_base_amount()

class account_invoice(models.Model):
    _inherit = "account.invoice"

    def _compute_amount(self):
        for inv in self:
            currency = inv.currency_id or None
            amount_total = 0
            for line in inv.invoice_line_ids:
                price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                taxes = line.invoice_line_tax_ids.with_context({'round':False}).compute_all(price, currency, line.quantity, product=line.product_id, partner=inv.partner_id)
                if taxes and taxes['total_included'] > 0:
                    amount_total += taxes['total_included']
                else:
                    amount_total += line.price_tax_included

            inv.amount_tax = sum(line.amount for line in inv.tax_line_ids)
            inv.amount_untaxed = currency.round(amount_total - inv.amount_tax)
            inv.amount_total = inv.amount_untaxed + inv.amount_tax
            amount_total_company_signed = inv.amount_total
            amount_untaxed_signed = inv.amount_untaxed
            if inv.currency_id and inv.currency_id != inv.company_id.currency_id:
                amount_total_company_signed = inv.currency_id.compute(inv.amount_total, inv.company_id.currency_id)
                amount_untaxed_signed = inv.currency_id.compute(inv.amount_untaxed, inv.company_id.currency_id)
            sign = inv.type in ['in_refund', 'out_refund'] and -1 or 1
            inv.amount_total_company_signed = amount_total_company_signed * sign
            inv.amount_total_signed = inv.amount_total * sign
            inv.amount_untaxed_signed = amount_untaxed_signed * sign


    def get_document_class_default(self, document_classes):
        if self.turn_issuer.vat_affected not in ['SI', 'ND']:
            exempt_ids = [
                self.env.ref('l10n_cl_invoice.dc_y_f_dtn').id,
                self.env.ref('l10n_cl_invoice.dc_y_f_dte').id]
            for document_class in document_classes:
                if document_class.sii_document_class_id.id in exempt_ids:
                    document_class_id = document_class.id
                    break
                else:
                    document_class_id = document_classes.ids[0]
        else:
            document_class_id = document_classes.ids[0]
        return document_class_id

    @api.onchange('journal_id', 'company_id')
    def _set_available_issuer_turns(self):
        for rec in self:
            if rec.company_id:
                available_turn_ids = rec.company_id.company_activities_ids
                for turn in available_turn_ids:
                    rec.turn_issuer = turn

    @api.multi
    def name_get(self):
        TYPES = {
            'out_invoice': _('Invoice'),
            'in_invoice': _('Supplier Invoice'),
            'out_refund': _('Refund'),
            'in_refund': _('Supplier Refund'),
        }
        result = []
        for inv in self:
            result.append(
                (inv.id, "%s %s" % (inv.document_number or TYPES[inv.type], inv.name or '')))
        return result

    @api.model
    def name_search(self, name, args=None, operator='ilike', limit=100):
        args = args or []
        recs = self.browse()
        if name:
            recs = self.search(
                [('document_number', '=', name)] + args, limit=limit)
        if not recs:
            recs = self.search([('name', operator, name)] + args, limit=limit)
        return recs.name_get()

    @api.onchange('journal_id', 'partner_id', 'turn_issuer','invoice_turn')
    def _get_available_journal_document_class(self):
        for inv in self:
            invoice_type = inv.type
            document_class_ids = []
            document_class_id = False

            inv.available_journal_document_class_ids = self.env[
                'account.journal.sii_document_class']
            if invoice_type in [
                    'out_invoice', 'in_invoice', 'out_refund', 'in_refund']:
                operation_type = inv.get_operation_type(invoice_type)

                if inv.use_documents:
                    letter_ids = inv.get_valid_document_letters(
                        inv.partner_id.id, operation_type, inv.company_id.id,
                        inv.turn_issuer.vat_affected, invoice_type)

                    domain = [
                        ('journal_id', '=', inv.journal_id.id),
                        '|', ('sii_document_class_id.document_letter_id',
                              'in', letter_ids),
                             ('sii_document_class_id.document_letter_id', '=', False)]

                    # If document_type in context we try to serch specific document
                    # document_type = self._context.get('document_type', False)
                    # en este punto document_type siempre es falso.
                    # TODO: revisar esta opcion
                    #document_type = self._context.get('document_type', False)
                    #if document_type:
                    #    document_classes = self.env[
                    #        'account.journal.sii_document_class'].search(
                    #        domain + [('sii_document_class_id.document_type', '=', document_type)])
                    #    if document_classes.ids:
                    #        # revisar si hay condicion de exento, para poner como primera alternativa estos
                    #        document_class_id = self.get_document_class_default(document_classes)
                    if invoice_type  in [ 'in_refund', 'out_refund']:
                        domain += [('sii_document_class_id.document_type','in',['debit_note','credit_note'] )]
                    else:
                        domain += [('sii_document_class_id.document_type','in',['invoice','invoice_in'] )]

                    # For domain, we search all documents
                    document_classes = self.env[
                        'account.journal.sii_document_class'].search(domain)
                    document_class_ids = document_classes.ids

                    # If not specific document type found, we choose another one
                    if not document_class_id and document_class_ids:
                        # revisar si hay condicion de exento, para poner como primera alternativa estos
                        # to-do: manejar más fino el documento por defecto.
                        document_class_id = inv.get_document_class_default(document_classes)
                # incorporado nuevo, para la compra
                if operation_type == 'purchase':
                    inv.available_journals = []

            inv.available_journal_document_class_ids = document_class_ids
            if not inv.journal_document_class_id:
                inv.journal_document_class_id = document_class_id

    @api.onchange('sii_document_class_id')
    def _check_vat(self):
        boleta_ids = [
            self.env.ref('l10n_cl_invoice.dc_bzf_f_dtn').id,
            self.env.ref('l10n_cl_invoice.dc_b_f_dtm').id]
        if self.sii_document_class_id not in boleta_ids and self.partner_id.document_number == '' or self.partner_id.document_number == '0':
            raise Warning(_("""The customer/supplier does not have a VAT \
defined. The type of invoicing document you selected requires you tu settle \
a VAT."""))

    @api.one
    @api.depends(
        'sii_document_class_id',
        'sii_document_class_id.document_letter_id',
        'sii_document_class_id.document_letter_id.vat_discriminated',
        'company_id',
        'company_id.invoice_vat_discrimination_default',)
    def get_vat_discriminated(self):
        vat_discriminated = False
        # agregarle una condicion: si el giro es afecto a iva, debe seleccionar factura, de lo contrario boleta (to-do)
        if self.sii_document_class_id.document_letter_id.vat_discriminated or self.company_id.invoice_vat_discrimination_default == 'discriminate_default':
            vat_discriminated = True
        self.vat_discriminated = vat_discriminated

    @api.one
    @api.depends('sii_document_number', 'number')
    def _get_document_number(self):
        if self.sii_document_number and self.sii_document_class_id:
            document_number = (
                self.sii_document_class_id.doc_code_prefix or '') + self.sii_document_number
        else:
            document_number = self.number
        self.document_number = document_number

    turn_issuer = fields.Many2one(
        'partner.activities',
        'Giro Emisor', readonly=True, store=True, required=False,
        states={'draft': [('readonly', False)]},
        )

    vat_discriminated = fields.Boolean(
        'Discriminate VAT?',
        compute="get_vat_discriminated",
        store=True,
        readonly=False,
        help="Discriminate VAT on Quotations and Sale Orders?")

    available_journals = fields.Many2one(
        'account.journal',
        compute='_get_available_journal_document_class',
        string='Available Journals')

    available_journal_document_class_ids = fields.Many2many(
        'account.journal.sii_document_class',
        compute='_get_available_journal_document_class',
        string='Available Journal Document Classes')

    supplier_invoice_number = fields.Char(
        copy=False)
    journal_document_class_id = fields.Many2one(
        'account.journal.sii_document_class',
        'Documents Type',
        default=_get_available_journal_document_class,
        readonly=True,
        store=True,
        states={'draft': [('readonly', False)]})
    sii_document_class_id = fields.Many2one(
        'sii.document_class',
        related='journal_document_class_id.sii_document_class_id',
        string='Document Type',
        copy=False,
        readonly=True,
        store=True)
    sii_document_number = fields.Char(
        string='Document Number',
        copy=False,
        readonly=True,)
    responsability_id = fields.Many2one(
        'sii.responsability',
        string='Responsability',
        related='commercial_partner_id.responsability_id',
        store=True,
        )
    formated_vat = fields.Char(
        string='Responsability',
        related='commercial_partner_id.formated_vat',)
    iva_uso_comun = fields.Boolean(string="Uso Común", readonly=True, states={'draft': [('readonly', False)]}) # solamente para compras tratamiento del iva
    no_rec_code = fields.Selection([
                    ('1','Compras destinadas a IVA a generar operaciones no gravados o exentas.'),
                    ('2','Facturas de proveedores registrados fuera de plazo.'),
                    ('3','Gastos rechazados.'),
                    ('4','Entregas gratuitas (premios, bonificaciones, etc.) recibidos.'),
                    ('9','Otros.')],
                    string="Código No recuperable",
                    readonly=True, states={'draft': [('readonly', False)]})# @TODO select 1 automático si es emisor 2Categoría

    document_number = fields.Char(
        compute='_get_document_number',
        string='Document Number',
        readonly=True,
    )
    next_invoice_number = fields.Integer(
        related='journal_document_class_id.sequence_id.number_next_actual',
        string='Next Document Number',
        readonly=True)
    use_documents = fields.Boolean(
        related='journal_id.use_documents',
        string='Use Documents?',
        readonly=True)
    referencias = fields.One2many('account.invoice.referencias','invoice_id', readonly=True, states={'draft': [('readonly', False)]})
    forma_pago = fields.Selection([('1','Contado'),('2','Crédito'),('3','Gratuito')],string="Forma de pago", readonly=True, states={'draft': [('readonly', False)]},
                    default='1')
    contact_id = fields.Many2one('res.partner', string="Contacto")

    @api.one
    @api.constrains('supplier_invoice_number', 'partner_id', 'company_id')
    def _check_reference(self):
        if self.type in ['out_invoice', 'out_refund'] and self.reference and self.state == 'open':
            domain = [('type', 'in', ('out_invoice', 'out_refund')),
                      # ('reference', '=', self.reference),
                      ('document_number', '=', self.document_number),
                      ('journal_document_class_id.sii_document_class_id', '=',
                       self.journal_document_class_id.sii_document_class_id.id),
                      ('company_id', '=', self.company_id.id),
                      ('id', '!=', self.id)]
            invoice_ids = self.search(domain)
            if invoice_ids:
                raise Warning(
                    _('Supplier Invoice Number must be unique per Supplier and Company!'))

    _sql_constraints = [
        ('number_supplier_invoice_number',
            'unique(supplier_invoice_number, partner_id, company_id)',
         'Supplier Invoice Number must be unique per Supplier and Company!'),
    ]

    @api.multi
    def action_move_create(self):
        for obj_inv in self:
            invtype = obj_inv.type
            if obj_inv.journal_document_class_id and not obj_inv.sii_document_number:
                if invtype in ('out_invoice', 'out_refund'):
                    if not obj_inv.journal_document_class_id.sequence_id:
                        raise osv.except_osv(_('Error!'), _(
                            'Please define sequence on the journal related documents to this invoice.'))
                    sii_document_number = obj_inv.journal_document_class_id.sequence_id.next_by_id()
                    prefix = obj_inv.journal_document_class_id.sii_document_class_id.doc_code_prefix or ''
                    move_name = (prefix + str(sii_document_number)).replace(' ','')
                    obj_inv.write({'move_name': move_name})
                elif invtype in ('in_invoice', 'in_refund'):
                    sii_document_number = obj_inv.supplier_invoice_number
        super(account_invoice, self).action_move_create()
        for obj_inv in self:
            invtype = obj_inv.type
            if obj_inv.journal_document_class_id and not obj_inv.sii_document_number:
                obj_inv.write({'sii_document_number': sii_document_number})
            document_class_id = obj_inv.sii_document_class_id.id
            guardar = {'document_class_id': document_class_id,
                'sii_document_number': obj_inv.sii_document_number,
                'no_rec_code':obj_inv.no_rec_code,
                'iva_uso_comun':obj_inv.iva_uso_comun,}
            obj_inv.move_id.write(guardar)
        return True

    def get_operation_type(self, cr, uid, invoice_type, context=None):
        if invoice_type in ['in_invoice', 'in_refund']:
            operation_type = 'purchase'
        elif invoice_type in ['out_invoice', 'out_refund']:
            operation_type = 'sale'
        else:
            operation_type = False
        return operation_type

    def get_valid_document_letters(
            self, cr, uid, partner_id, operation_type='sale',
            company_id=False, vat_affected='SI', invoice_type='out_invoice', context=None):
        if context is None:
            context = {}

        document_letter_obj = self.pool.get('sii.document_letter')
        user = self.pool.get('res.users').browse(cr, uid, uid, context=context)
        partner = self.pool.get('res.partner').browse(
            cr, uid, partner_id, context=context)

        if not partner_id or not company_id or not operation_type:
            return []

        partner = partner.commercial_partner_id

        if not company_id:
            company_id = context.get('company_id', user.company_id.id)
        company = self.pool.get('res.company').browse(
            cr, uid, company_id, context)

        if operation_type == 'sale':
            issuer_responsability_id = company.partner_id.responsability_id.id
            receptor_responsability_id = partner.responsability_id.id
            if invoice_type == 'out_invoice':
                if vat_affected == 'SI':
                    domain = [
                        ('issuer_ids', '=', issuer_responsability_id),
                        ('receptor_ids', '=', receptor_responsability_id),
                        ('name', '!=', 'C')]
                else:
                    domain = [
                        ('issuer_ids', '=', issuer_responsability_id),
                        ('receptor_ids', '=', receptor_responsability_id),
                        ('name', '=', 'C')]
            else:
                # nota de credito de ventas
                domain = [
                    ('issuer_ids', '=', issuer_responsability_id),
                    ('receptor_ids', '=', receptor_responsability_id)]
        elif operation_type == 'purchase':
            issuer_responsability_id = partner.responsability_id.id
            receptor_responsability_id = company.partner_id.responsability_id.id
            if invoice_type == 'in_invoice':
                print('responsabilidad del partner')
                if issuer_responsability_id == self.pool.get(
                        'ir.model.data').get_object_reference(
                        cr, uid, 'l10n_cl_invoice', 'res_BH')[1]:
                    print('el proveedor es de segunda categoria y emite boleta de honorarios')
                else:
                    print('el proveedor es de primera categoria y emite facturas o facturas no afectas')
                domain = [
                    ('issuer_ids', '=', issuer_responsability_id),
                    ('receptor_ids', '=', receptor_responsability_id)]
            else:
                # nota de credito de compras
                domain = ['|',('issuer_ids', '=', issuer_responsability_id),
                              ('receptor_ids', '=', receptor_responsability_id)]
        else:
            raise except_orm(_('Operation Type Error'),
                             _('Operation Type Must be "Sale" or "Purchase"'))

        # TODO: fijar esto en el wizard, o llamar un wizard desde aca
        # if not company.partner_id.responsability_id.id:
        #     raise except_orm(_('You have not settled a tax payer type for your\
        #      company.'),
        #      _('Please, set your company tax payer type (in company or \
        #      partner before to continue.'))

        document_letter_ids = document_letter_obj.search(
            cr, uid, domain, context=context)
        return document_letter_ids

class Referencias(models.Model):
    _name = 'account.invoice.referencias'

    origen = fields.Char(string="Origin")
    sii_referencia_TpoDocRef =  fields.Many2one('sii.document_class',
        string="SII Reference Document Type")
    sii_referencia_CodRef = fields.Selection(
        [('1','Anula Documento de Referencia'),('2','Corrige texto Documento Referencia'),('3','Corrige montos')],
        string="SII Reference Code")
    motivo = fields.Char(string="Motivo")
    invoice_id = fields.Many2one('account.invoice', ondelete='cascade',index=True,copy=False,string="Documento")
    fecha_documento = fields.Date(string="Fecha Documento", required=True)
