/*global $, braintree, waitingDialog, gettext*/
$(function () {
    var authorization = $.trim($("#braintree-token").text());

    braintree.client.create(
        {
            authorization: authorization
        },
        function (clientErr, clientInstance) {
            if (clientErr) {
                // Handle error in client creation
                console.error(clientErr);
                return;
            }

            braintree.hostedFields.create(
                {
                    client: clientInstance,
                    styles: {
                        'input': {
                            'font-size': '14px',
                            'font-family': '"Open Sans", "OpenSans", "Helvetica Neue", Helvetica, Arial, sans-serif',
                            'color': '#3a3a3a'
                        },
                        ':focus': {
                            'color': 'black'
                        },
                    },
                    fields: {
                        number: {
                            selector: '#bt-card-number',
                            placeholder: '4111 1111 1111 1111'
                        },
                        cvv: {
                            selector: '#bt-cvv',
                            placeholder: '123'
                        },
                        expirationDate: {
                            selector: '#bt-expiration-date',
                            placeholder: '10/2019'
                        }
                    }
                },
                function (hostedFieldsErr, hostedFieldsInstance) {
                    if (hostedFieldsErr) {
                        // Handle error in Hosted Fields creation
                        return;
                    }

                    $("#braintree_nonce").parents("form").submit(
                        function () {
                            if (($("input[name=payment][value=braintreecc]").prop('checked') || $("input[name=payment]").length === 0)
                                && $("#stripe_token").val() === "") {

                                waitingDialog.show(gettext("Contacting Braintree …"));
                                event.preventDefault();

                                hostedFieldsInstance.tokenize(function (tokenizeErr, payload) {
                                    waitingDialog.hide();
                                    if (tokenizeErr) {
                                        console.error(tokenizeErr.message);
                                        $(".braintree-errors").stop().hide().removeClass("sr-only");
                                        $(".braintree-errors").html("<div class='alert alert-danger'>" + tokenizeErr + "</div>");
                                        $(".braintree-errors").slideDown();
                                        return;
                                    }

                                    $('#braintree_nonce').val(payload.nonce);
                                    $('#braintree_nonce').parents("form").get(0).submit();
                                });

                                return false;
                            }
                        }
                    );
                });
        });
});
