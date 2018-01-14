from decimal import Decimal
import logging
import json
import sys

import stripe

from django.conf import settings
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def get_customer_token(user):
    new_customer = stripe.Customer.create(
        source=user.stripe_card,
        email=user.email
    )
    if new_customer:
        return new_customer.id


class StripeAccount(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    account_id = models.CharField(max_length=100, blank=True)
    secret_key = models.CharField(max_length=100, blank=True, editable=False)
    public_key = models.CharField(max_length=100, blank=True)
    available_balance = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    verification = models.TextField(default='', blank=True)

    def identity_verified(self):
        # codesy verification field holds verification requirement from stripe
        # identity is verified if verification is blank or due_by is None

        # first, see if an account_id has been assigned by stripe
        # and check on verification
        if self.account_id:
            stripe_account = stripe.Account.retrieve(self.account_id)
            self.verification = json.dumps(stripe_account.verification)
            self.save()
        else:
            # returning True will allow the bank account_id to be assigned
            return True
        if not self.verification:
            return True
        else:
            verification = json.loads(self.verification)
            return not verification['due_by']

    @property
    def fields_needed(self):
        try:
            verification = json.loads(self.verification)['fields_needed']
        except ValueError:
            verification = json.loads('{}')
        return verification

    def new_managed_account(self, bank_account):
        stripe_account = stripe.Account.create(
            country="US",
            managed=True,
            tos_acceptance={
                'date': self.user.tos_acceptance_date,
                'ip': self.user.tos_acceptance_ip,
            },
            external_account=bank_account
        )
        if stripe_account:
            self.account_id = stripe_account['id']
            self.secret_key = stripe_account['keys']['secret']
            self.public_key = stripe_account['keys']['publishable']
            self.save()

    def external_account(self, bank_account):
        if not self.account_id:
            self.new_managed_account(bank_account)
        else:
            stripe_account = stripe.Account.retrieve(self.account_id)
            stripe_account.external_account = bank_account
            stripe_account.save()


class StripeEvent(models.Model):
    event_id = models.CharField(primary_key=True, max_length=100, blank=True)
    user_id = models.CharField(max_length=100, blank=True)
    type = models.CharField(max_length=100, blank=True)
    message_text = models.TextField()
    verified = models.BooleanField(default=False)
    processed = models.BooleanField(default=False)
    created = models.DateTimeField(null=True, blank=True)

    @property
    def message(self):
        return json.loads(self.message_text)

    def save(self, *args, **kwargs):
        self.created = timezone.now()
        super(StripeEvent, self).save(*args, **kwargs)

        try:
            self.user_id = self.message_text['user_id']
            retrieved_event = stripe.Event.retrieve(
                id=self.event_id, stripe_account=self.user_id)
        except KeyError:
            retrieved_event = stripe.Event.retrieve(id=self.event_id)

        if retrieved_event:
            self.verified = True
            self.type = retrieved_event['type']
            # indent used to avoid c encoder bug
            self.message_text = json.dumps(retrieved_event, indent=4)
            super(StripeEvent, self).save(*args, **kwargs)
        else:
            # TODO: log this somewhere
            return

        # attempt to process a webhook for the event type
        try:
            webhooks[self.type](event=self).process()
            self.processed = True
        except KeyError:
            # TODO: Add something like papertrail to log this
            pass
        super(StripeEvent, self).save(*args, **kwargs)


webhooks = {}


class WebHookProcessor(object):

    def __init__(self, event, *args, **kwargs):
        self.object = event.message['data']['object']
        super(WebHookProcessor, self).__init__(*args, **kwargs)

    def process(self):
        raise NotImplementedError


class AccountUpdatedProcessor(WebHookProcessor):
    def process(self):
        try:
            account = StripeAccount.objects.get(account_id=self.object['id'])
            verification = (
                self.object['verification']
            )
            account.verification = json.dumps(verification)
            account.save()
        except StripeAccount.DoesNotExist:
            # TODO: Tell someone this happened
            pass


webhooks['account.updated'] = AccountUpdatedProcessor


class BalanceAvailableProcessor(WebHookProcessor):
    def process(self):
        try:
            account = StripeAccount.objects.get(account_id=self.object['id'])
            amount = Decimal(
                self.object['available'][0]['amount'])
            account.available_balance = (amount / 100)
            account.save()
        except StripeAccount.DoesNotExist:
            pass


webhooks['balance.available'] = BalanceAvailableProcessor


class ChargeUpdatedProcessor(WebHookProcessor):
    def process(self):
        try:
            logger.error("Unhandled charge.updated web-hook call")
        except:
            e = sys.exc_info()[0]
            logger.error("exception in unhandled charge.updated web-hook call:"
                         "%s" % e)


webhooks['charge.updated'] = ChargeUpdatedProcessor


class PaymentCreatedProcessor(WebHookProcessor):
    def process(self):
        try:
            logger.error("Unhandled payment.created webhook call")
        except:
            e = sys.exc_info()[0]
            logger.error("exception in unhandled payment.created webhook call:"
                         "%s" % e)


webhooks['payment.created'] = PaymentCreatedProcessor
