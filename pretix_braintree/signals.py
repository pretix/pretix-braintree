import json

from django.core.urlresolvers import resolve
from django.dispatch import receiver
from django.http import HttpRequest, HttpResponse
from django.template.loader import get_template
from django.utils.translation import ugettext_lazy as _

from pretix.base.middleware import _parse_csp, _merge_csp, _render_csp
from pretix.base.signals import (
    logentry_display, register_payment_providers, requiredaction_display,
)
from pretix.presale.signals import html_head, process_response


@receiver(register_payment_providers, dispatch_uid="payment_braintree")
def register_payment_provider(sender, **kwargs):
    from .payment import BraintreeCC

    return BraintreeCC


@receiver(html_head, dispatch_uid="payment_braintree_html_head")
def html_head_presale(sender, request=None, **kwargs):
    from .payment import BraintreeCC

    provider = BraintreeCC(sender)
    url = resolve(request.path_info)
    if provider.is_enabled and ("checkout" in url.url_name or "order.pay" in url.url_name):
        template = get_template('pretix_braintree/presale_head.html')
        ctx = {'event': sender, 'settings': provider.settings}
        return template.render(ctx)
    else:
        return ""


@receiver(signal=process_response, dispatch_uid="braintree_middleware_resp")
def signal_process_response(sender, request: HttpRequest, response: HttpResponse, **kwargs):
    from .payment import BraintreeCC

    provider = BraintreeCC(sender)
    url = resolve(request.path_info)
    if provider.is_enabled and ("checkout" in url.url_name or "order.pay" in url.url_name):
        if 'Content-Security-Policy' in response:
            h = _parse_csp(response['Content-Security-Policy'])
        else:
            h = {}

        _merge_csp(h, {
            'script-src': ['js.braintreegateway.com', 'assets.braintreegateway.com', 'www.paypalobjects.com'],
            'img-src': ['assets.braintreegateway.com', 'checkout.paypal.com', 'data:'],
            'child-src': ['assets.braintreegateway.com', 'c.paypal.com'],
            'frame-src': ['assets.braintreegateway.com', 'c.paypal.com'],
            'connect-src': ['api.sandbox.braintreegateway.com', 'api.braintreegateway.com',
                            'client-analytics.braintreegateway.com', 'client-analytics.sandbox.braintreegateway.com'],
        })

        if h:
            response['Content-Security-Policy'] = _render_csp(h)
    return response
