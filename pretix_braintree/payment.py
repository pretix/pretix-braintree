import json
import logging
from collections import OrderedDict

import braintree
from braintree.exceptions.braintree_error import BraintreeError
from django import forms
from django.contrib import messages
from django.template.loader import get_template
from django.utils.translation import ugettext_lazy as _

from pretix.base.models import Quota, RequiredAction
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.services.mail import SendMailException
from pretix.base.services.orders import mark_order_paid, mark_order_refunded

logger = logging.getLogger(__name__)


class BraintreeCC(BasePaymentProvider):
    identifier = 'braintreecc'
    verbose_name = _('Credit Card via Braintree')

    @property
    def settings_form_fields(self):
        return OrderedDict(
            list(super().settings_form_fields.items()) + [
                ('merchant_id',
                 forms.CharField(
                     label=_('Merchant ID'),
                 )),
                ('public_key',
                 forms.CharField(
                     label=_('Public key'),
                 )),
                ('private_key',
                 forms.CharField(
                     label=_('Private key'),
                 )),
                ('environment',
                 forms.ChoiceField(
                     label=_('Environment'),
                     initial='production',
                     choices=(
                         ('production', 'Production'),
                         ('sandbox', 'Sandbox'),
                     ),
                 )),
            ]
        )

    def payment_is_valid_session(self, request):
        return request.session.get('payment_braintree_nonce', '') != ''

    def order_prepare(self, request, order):
        return self.checkout_prepare(request, None)

    def checkout_prepare(self, request, cart):
        token = request.POST.get('payment_braintree_nonce', '')
        if token == '':
            messages.error(request, _('You may need to enable JavaScript for credit card payments.'))
            return False
        request.session['payment_braintree_nonce'] = token
        return True

    def payment_form_render(self, request) -> str:
        self._init_api()

        template = get_template('pretix_braintree/checkout_payment_form.html')
        ctx = {
            'request': request,
            'event': self.event,
            'settings': self.settings,
            'client_token': braintree.ClientToken.generate()
        }
        return template.render(ctx)

    def _init_api(self):
        braintree.Configuration.configure(braintree.Environment.Sandbox
                                          if self.settings.get('environment') == 'sandbox'
                                          else braintree.Environment.Production,
                                          merchant_id=self.settings.get('merchant_id'),
                                          public_key=self.settings.get('public_key'),
                                          private_key=self.settings.get('private_key'))

    def checkout_confirm_render(self, request) -> str:
        template = get_template('pretix_braintree/checkout_payment_confirm.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings}
        return template.render(ctx)

    def order_can_retry(self, order):
        return self._is_still_available()

    def _serialize(self, transaction):
        return {
            'amount': str(transaction.amount),
            'card_type': transaction.credit_card_details.card_type if transaction.credit_card_details else None,
            'card_masked_number': (transaction.credit_card_details.masked_number
                                   if transaction.credit_card_details else None),
            'gateway_rejection_reason': transaction.gateway_rejection_reason,
            'id': transaction.id,
            'merchant_account_id': transaction.merchant_account_id,
            'order_id': transaction.order_id,
            'payment_instrument_type': transaction.payment_instrument_type,
            'processor_response_code': transaction.processor_response_code,
            'processor_response_text': transaction.processor_response_text,
            'processor_settlement_response_code': transaction.processor_settlement_response_code,
            'processor_settlement_response_text': transaction.processor_settlement_response_text,
            'refund_ids': transaction.refund_ids,
            'status': transaction.status,
            'type': transaction.type,
            'updated_at': transaction.updated_at.isoformat(),
            'created_at': transaction.created_at.isoformat(),

        }

    def payment_perform(self, request, order) -> str:
        self._init_api()
        result = braintree.Transaction.sale({
            "amount": str(order.total),
            "payment_method_nonce": request.session['payment_braintree_nonce'],
            "options": {
                "submit_for_settlement": True
            }
        })
        if result.is_success:
            try:
                mark_order_paid(order, self.identifier, json.dumps(self._serialize(result.transaction)))
            except Quota.QuotaExceededException as e:
                RequiredAction.objects.create(
                    event=request.event, action_type='pretix_braintree.overpaid', data=json.dumps({
                        'order': order.code,
                    })
                )
                raise PaymentException(str(e))

            except SendMailException:
                raise PaymentException(_('There was an error sending the confirmation mail.'))
        else:
            if result.transaction:
                order.payment_info = json.dumps(self._serialize(result.transaction))
            else:
                order.payment_info = json.dumps({'error': result.message})
            order.save()
            raise PaymentException(
                _('Your payment failed because Braintree reported the following error: %s') % str(result.message)
            )

        del request.session['payment_braintree_nonce']

    def order_pending_render(self, request, order) -> str:
        if order.payment_info:
            payment_info = json.loads(order.payment_info)
        else:
            payment_info = None
        template = get_template('pretix_braintree/pending.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings,
               'order': order, 'payment_info': payment_info}
        return template.render(ctx)

    def order_control_render(self, request, order) -> str:
        if order.payment_info:
            payment_info = json.loads(order.payment_info)
        else:
            payment_info = None
        template = get_template('pretix_braintree/control.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings,
               'payment_info': payment_info, 'order': order}
        return template.render(ctx)

    def order_control_refund_render(self, order) -> str:
        return '<div class="alert alert-info">%s</div>' % _('The money will be automatically refunded.')

    def order_control_refund_perform(self, request, order) -> "bool|str":
        self._init_api()

        if order.payment_info:
            payment_info = json.loads(order.payment_info)
        else:
            payment_info = None

        if not payment_info or 'id' not in payment_info:
            mark_order_refunded(order, user=request.user)
            messages.warning(request, _('We were unable to transfer the money back automatically. '
                                        'Please get in touch with the customer and transfer it back manually.'))
            return

        try:
            transaction = braintree.Transaction.find(payment_info['id'])
            if transaction.status in ("authorized", "submitted_for_settlement"):
                result = braintree.Transaction.void(payment_info['id'])
            elif transaction.status in ("settled", "settling"):
                result = braintree.Transaction.refund(payment_info['id'])
            else:
                mark_order_refunded(order, user=request.user)
                logger.warning('Braintree refund of invalid state requested: %s' % transaction.status)
                messages.warning(request, _('We were unable to transfer the money back automatically. '
                                            'Please get in touch with the customer and transfer it back manually.'))
                return

            if result.is_success:
                transaction = braintree.Transaction.find(payment_info['id'])
                order = mark_order_refunded(order, user=request.user)
                order.payment_info = json.dumps(self._serialize(transaction))
                order.save()
            else:
                order = mark_order_refunded(order, user=request.user)
                logger.warning('Braintree refund/void failed: %s' % result.message)
                messages.warning(request, _('We were unable to transfer the money back automatically. '
                                            'Please get in touch with the customer and transfer it back manually.'))
        except BraintreeError as e:
            logger.exception('Braintree error: %s' % str(e))
            mark_order_refunded(order, user=request.user)
            messages.warning(request, _('We were unable to transfer the money back automatically. '
                                        'Please get in touch with the customer and transfer it back manually.'))
