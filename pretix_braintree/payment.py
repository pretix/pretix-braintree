import json
import logging
from collections import OrderedDict

import braintree
from django import forms
from django.contrib import messages
from django.template.loader import get_template
from django.utils.translation import ugettext_lazy as _

from pretix.base.models import Quota, RequiredAction
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.services.mail import SendMailException
from pretix.base.services.orders import mark_order_paid, mark_order_refunded
from pretix.multidomain.urlreverse import build_absolute_uri

logger = logging.getLogger('pretix.plugins.stripe')


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
                mark_order_paid(order, self.identifier, result.transaction.id)
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
            raise PaymentException(str(result.deep_errors))

        del request.session['payment_braintree_nonce']

    def order_pending_render(self, request, order) -> str:
        if order.payment_info:
            payment_info = json.loads(order.payment_info)
        else:
            payment_info = None
        template = get_template('pretixplugins/stripe/pending.html')
        ctx = {'request': request, 'event': self.event, 'settings': self.settings,
               'order': order, 'payment_info': payment_info}
        return template.render(ctx)

    def order_control_render(self, request, order) -> str:
        if order.payment_info:
            payment_info = json.loads(order.payment_info)
            if 'amount' in payment_info:
                payment_info['amount'] /= 100
        else:
            payment_info = None
        template = get_template('pretixplugins/stripe/control.html')
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

        if not payment_info:
            mark_order_refunded(order, user=request.user)
            messages.warning(request, _('We were unable to transfer the money back automatically. '
                                        'Please get in touch with the customer and transfer it back manually.'))
            return

        try:
            ch = stripe.Charge.retrieve(payment_info['id'])
            ch.refunds.create()
            ch.refresh()
        except (stripe.error.InvalidRequestError, stripe.error.AuthenticationError, stripe.error.APIConnectionError) \
                as e:
            if e.json_body:
                err = e.json_body['error']
                logger.exception('Stripe error: %s' % str(err))
            else:
                err = {'message': str(e)}
                logger.exception('Stripe error: %s' % str(e))
            messages.error(request, _('We had trouble communicating with Stripe. Please try again and contact '
                                      'support if the problem persists.'))
            logger.error('Stripe error: %s' % str(err))
        except stripe.error.StripeError:
            mark_order_refunded(order, user=request.user)
            messages.warning(request, _('We were unable to transfer the money back automatically. '
                                        'Please get in touch with the customer and transfer it back manually.'))
        else:
            order = mark_order_refunded(order, user=request.user)
            order.payment_info = str(ch)
            order.save()
