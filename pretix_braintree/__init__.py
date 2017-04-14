from django.apps import AppConfig


class PluginApp(AppConfig):
    name = 'pretix_braintree'
    verbose_name = 'pretix braintree payments'

    class PretixPluginMeta:
        name = 'pretix braintree payments'
        author = 'Raphael Michel'
        description = 'Braintree payment provider for pretix'
        visible = True
        version = '1.0.0'

    def ready(self):
        from . import signals  # NOQA


default_app_config = 'pretix_braintree.PluginApp'
