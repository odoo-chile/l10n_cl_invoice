# -*- coding: utf-8 -*-
from openerp.osv import fields
from openerp import fields as new_fields
from openerp import api, models, _
from openerp.exceptions import Warning


class sii_tax_code(models.Model):
    _inherit = 'account.tax'

    sii_code = new_fields.Integer('SII Code')
    sii_type = new_fields.Selection([ ('A','Anticipado'),('R','Retención')], string="Tipo de impuesto para el SII")
    retencion = new_fields.Float(string="Valor retención", default=0.00)

    @api.v8
    def compute_all(self, price_unit, currency=None, quantity=1.0, product=None, partner=None):
        """ Returns all information required to apply taxes (in self + their children in case of a tax goup).
            We consider the sequence of the parent for group of taxes.
                Eg. considering letters as taxes and alphabetic order as sequence :
                [G, B([A, D, F]), E, C] will be computed as [A, D, F, C, E, G]
        RETURN: {
            'total_excluded': 0.0,    # Total without taxes
            'total_included': 0.0,    # Total with taxes
            'taxes': [{               # One dict for each tax in self and their children
                'id': int,
                'name': str,
                'amount': float,
                'sequence': int,
                'account_id': int,
                'refund_account_id': int,
                'analytic': boolean,
            }]
        } """
        if len(self) == 0:
            company_id = self.env.user.company_id
        else:
            company_id = self[0].company_id
        if not currency:
            currency = company_id.currency_id
        taxes = []
        # By default, for each tax, tax amount will first be computed
        # and rounded at the 'Account' decimal precision for each
        # PO/SO/invoice line and then these rounded amounts will be
        # summed, leading to the total amount for that tax. But, if the
        # company has tax_calculation_rounding_method = round_globally,
        # we still follow the same method, but we use a much larger
        # precision when we round the tax amount for each line (we use
        # the 'Account' decimal precision + 5), and that way it's like
        # rounding after the sum of the tax amounts of each line
        prec = currency.decimal_places

        total_excluded = total_included = base = round(price_unit * quantity, prec)

        if company_id.tax_calculation_rounding_method == 'round_globally' or not bool(self.env.context.get("round", True)):
            prec += 5

        # Sorting key is mandatory in this case. When no key is provided, sorted() will perform a
        # search. However, the search method is overridden in account.tax in order to add a domain
        # depending on the context. This domain might filter out some taxes from self, e.g. in the
        # case of group taxes.
        for tax in self.sorted(key=lambda r: r.sequence):
            if tax.amount_type == 'group':
                ret = tax.children_tax_ids.compute_all(price_unit, currency, quantity, product, partner)
                total_excluded = ret['total_excluded']
                base = ret['base']
                total_included = ret['total_included']
                tax_amount = total_included - total_excluded
                taxes += ret['taxes']
                continue

            tax_amount = tax._compute_amount(base, price_unit, quantity, product, partner)
            if company_id.tax_calculation_rounding_method == 'round_globally' or not bool(self.env.context.get("round", True)):
                tax_amount = round(tax_amount, prec)
            else:
                tax_amount = currency.round(tax_amount)

            if tax.price_include:
                total_excluded -= tax_amount
                base -= tax_amount
            else:
                total_included += tax_amount

            # Keep base amount used for the current tax
            tax_base = base

            if tax.include_base_amount:
                base += tax_amount

            taxes.append({
                'id': tax.id,
                'name': tax.with_context(**{'lang': partner.lang} if partner else {}).name,
                'amount': tax_amount,
                'base': tax_base,
                'sequence': tax.sequence,
                'account_id': tax.account_id.id,
                'refund_account_id': tax.refund_account_id.id,
                'analytic': tax.analytic,
            })


        return {
            'taxes': sorted(taxes, key=lambda k: k['sequence']),
            'total_excluded': currency.round(total_excluded) if bool(self.env.context.get("round", True)) else total_excluded,
            'total_included': currency.round(total_included) if bool(self.env.context.get("round", True)) else total_included,
            'base': base,
            }

    def _compute_amount(self, base_amount, price_unit, quantity=1.0, product=None, partner=None):
        if self.amount_type == 'percent' and self.price_include:
            neto = round(base_amount / (1 + self.amount / 100))
            iva_round =  round(neto * ( self.amount / 100))
            if round(neto+iva_round) != round(base_amount):
                neto = int(base_amount / (1 + self.amount / 100))
            iva = base_amount - neto
            return iva
        return super(sii_tax_code,self)._compute_amount(base_amount, price_unit, quantity, product, partner)

class account_move(models.Model):
    _inherit = "account.move"

    def _get_document_data(self, cr, uid, ids, name, arg, context=None):
        """ TODO """
        res = {}
        for record in self.browse(cr, uid, ids, context=context):
            document_number = False
            if record.model and record.res_id:
                document_number = self.pool[record.model].browse(
                    cr, uid, record.res_id, context=context).document_number
            res[record.id] = document_number
        return res

    @api.one
    @api.depends(
        'sii_document_number',
        'name',
        'document_class_id',
        'document_class_id.doc_code_prefix',
        )
    def _get_document_number(self):
        if self.sii_document_number and self.document_class_id:
            document_number = (self.document_class_id.doc_code_prefix or '') + self.sii_document_number
        else:
            document_number = self.name
        self.document_number = document_number

    document_class_id = new_fields.Many2one(
        'sii.document_class',
        'Document Type',
        copy=False,
        readonly=True, states={'draft': [('readonly', False)]}
    )
    sii_document_number = new_fields.Char(
        string='Document Number',
        copy=False,readonly=True, states={'draft': [('readonly', False)]})

    canceled = new_fields.Boolean(string="Canceled?", readonly=True, states={'draft': [('readonly', False)]})
    iva_uso_comun = new_fields.Boolean(string="Iva Uso Común", readonly=True, states={'draft': [('readonly', False)]})
    no_rec_code = new_fields.Selection([
                    ('1','Compras destinadas a IVA a generar operaciones no gravados o exentas.'),
                    ('2','Facturas de proveedores registrados fuera de plazo.'),
                    ('3','Gastos rechazados.'),
                    ('4','Entregas gratuitas (premios, bonificaciones, etc.) recibidos.'),
                    ('9','Otros.')],
                    string="Código No recuperable",
                    readonly=True, states={'draft': [('readonly', False)]})# @TODO select 1 automático si es emisor 2Categoría

    document_number = new_fields.Char(
        compute='_get_document_number',
        string='Document Number',
        store=True,
        readonly=True, states={'draft': [('readonly', False)]})
    sended = new_fields.Boolean(string="Enviado al SII", default=False,readonly=True, states={'draft': [('readonly', False)]})
    factor_proporcionalidad = new_fields.Float(string="Factor proporcionalidad", default=0.00, readonly=True, states={'draft': [('readonly', False)]})


class account_move_line(models.Model):
    _inherit = "account.move.line"

    document_class_id = new_fields.Many2one(
        'sii.document_class',
        'Document Type',
        related='move_id.document_class_id',
        store=True,
        readonly=True,
    )
    document_number = new_fields.Char(
        string='Document Number',
        related='move_id.document_number',
        store=True,
        readonly=True,
        )

class account_journal_sii_document_class(models.Model):
    _name = "account.journal.sii_document_class"
    _description = "Journal SII Documents"

    def name_get(self, cr, uid, ids, context=None):
        result = []
        for record in self.browse(cr, uid, ids, context=context):
            result.append((record.id, record.sii_document_class_id.name))
        return result

    _order = 'journal_id desc, sequence, id'

    sii_document_class_id = new_fields.Many2one('sii.document_class',
                                                'Document Type', required=True)
    sequence_id = new_fields.Many2one(
        'ir.sequence', 'Entry Sequence', required=False,
        help="""This field contains the information related to the numbering \
        of the documents entries of this document type.""")
    journal_id = new_fields.Many2one(
        'account.journal', 'Journal', required=True)
    sequence = new_fields.Integer('Sequence',)


class account_journal(models.Model):
    _inherit = "account.journal"

    #_columns = {
    #    'point_of_sale': fields.related(
    #        'point_of_sale_id', 'number', type='integer',
    #        string='Point of sale', readonly=True),  # for compatibility
    #}


    journal_document_class_ids = new_fields.One2many(
            'account.journal.sii_document_class', 'journal_id',
            'Documents Class',)

    point_of_sale_id = new_fields.Many2one('sii.point_of_sale','Point of sale')

    point_of_sale = new_fields.Integer(
        related='point_of_sale_id.number', string='Point of sale',
        readonly=True)

    use_documents = new_fields.Boolean(
        'Use Documents?', default='_get_default_doc')

    document_sequence_type = new_fields.Selection(
            [('own_sequence', 'Own Sequence'),
             ('same_sequence', 'Same Invoice Sequence')],
            string='Document Sequence Type',
            help="Use own sequence or invoice sequence on Debit and Credit \
                 Notes?")

    journal_activities_ids = new_fields.Many2many(
            'partner.activities',id1='journal_id', id2='activities_id',
            string='Journal Turns', help="""Select the turns you want to \
            invoice in this Journal""")

    excempt_documents = new_fields.Boolean(
        'Excempt Documents Available', compute='_check_activities')


    @api.multi
    def _get_default_doc(self):
        self.ensure_one()
        if 'sale' in self.type or 'purchase' in self.type:
            self.use_documents = True

    @api.one
    @api.depends('journal_activities_ids', 'type')
    def _check_activities(self):
        # self.ensure_one()
        # si entre los giros del diario hay alguno que está excento
        # el boolean es True
        try:
            if 'purchase' in self.type:
                self.excempt_documents = True
            elif 'sale' in self.type:
                no_vat = False
                for turn in self.journal_activities_ids:
                    print('turn %s' % turn.vat_affected)
                    if turn.vat_affected == 'SI':
                        continue
                    else:
                        no_vat = True
                        break
                self.excempt_documents = no_vat
        except:
            pass

    @api.one
    @api.constrains('point_of_sale_id', 'company_id')
    def _check_company_id(self):
        if self.point_of_sale_id and self.point_of_sale_id.company_id != self.company_id:
            raise Warning(_('The company of the point of sale and of the \
                journal must be the same!'))

class res_currency(models.Model):
    _inherit = "res.currency"
    sii_code = new_fields.Char('SII Code', size=4)


class partnerActivities(models.Model):
    _inherit = 'partner.activities'
    journal_ids = new_fields.Many2many(
        'account.journal', id1='activities_id', id2='journal_id',
        string='Journals')
