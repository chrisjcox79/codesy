import uuid
import requests
import re
import HTMLParser
from decimal import Decimal

from datetime import timedelta
from django.utils import timezone

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import get_template


from mailer import send_mail

from .managers import ClaimManager

import payments.utils as payments


def uuid_please():
    full_uuid = uuid.uuid4()
    # uuid is truncated because paypal must be <30
    return str(full_uuid)[:25]


class Bid(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    url = models.URLField()
    title = models.CharField(max_length=255, null=True, blank=True)
    issue = models.ForeignKey('Issue', null=True)
    ask = models.DecimalField(max_digits=6, decimal_places=2, blank=True,
                              default=0)
    ask_match_sent = models.DateTimeField(null=True, blank=True)
    offer = models.DecimalField(max_digits=6, decimal_places=2, blank=True,
                                default=0)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("user", "url"),)

    def __unicode__(self):
        return u'%s bid on %s' % (self.user, self.url)

    def ask_met(self):

        if self.ask:
            other_bids = Bid.objects.filter(
                url=self.url
            ).exclude(
                user=self.user
            ).aggregate(
                Sum('offer')
            )
            return other_bids['offer__sum'] >= self.ask
        else:
            return False

    @property
    def last_offer(self):
        try:
            return Offer.objects.filter(bid=self).order_by('modified')[:1][0]
        except IndexError:
            return None

    @property
    def offers(self):
        return Offer.objects.filter(bid=self)

    @property
    def activity(self):
        if self.offer > 0:
            return "OFFER"
        if self.ask > 0:
            return "ASK"
        return "NADA"

    def set_offer(self, offer_amount):
        offer_amount = Decimal(offer_amount)
        # refund any previous offers
        previous_offers = self.offers.exclude(
            charge_id=u''
        ).filter(
            refund_id=u''
        )
        if not previous_offers:
            self.offer = offer_amount
            self.save()
        else:
            for offer in previous_offers:
                payments.refund(offer)

        new_offer = Offer(
            user=self.user,
            amount=offer_amount,
            bid=self,
        )
        new_offer.save()
        payments.authorize(new_offer)
        self.offer = offer_amount
        self.save()
        return new_offer

    def claim_for_user(self, user):
        return Claim.objects.get(user=user, issue=self.issue)

    def claims_for_other_users(self, user):
        return Claim.objects.filter(issue=self.issue).exclude(user=user)

    def actionable_claims(self, user):
        """
        Returns claims for this bid on which the user can take some action:
            * own_claim: they may request payout
            * other_claims: they may vote
        """
        try:
            own_claim = self.claim_for_user(user)
        except Claim.DoesNotExist:
            own_claim = None

        other_claims = self.claims_for_other_users(user)
        return {'own_claim': own_claim, 'other_claims': other_claims}

    def is_biddable_by(self, user):
        nobid_claim_statuses = ['Submitted', 'Pending', 'Approved', 'Paid']
        if user == self.user and self.ask_met():
            return False
        actionable_claims = self.actionable_claims(user)
        own_claim = actionable_claims['own_claim']
        if own_claim and own_claim.status == 'Rejected':
            return True
        if own_claim and own_claim.status in nobid_claim_statuses:
            return False
        for other_claim in actionable_claims['other_claims']:
            if other_claim.status in nobid_claim_statuses:
                return False
        return True


@receiver(post_save, sender=Bid)
def notify_matching_askers(sender, instance, **kwargs):
    email_template = get_template('../templates/email/ask_met.html')
    unnotified_asks = Bid.objects.filter(
        url=instance.url, ask_match_sent=None).exclude(ask__lte=0)

    for bid in unnotified_asks:
        if bid.ask_met():
            email_context = {'ask': bid.ask, 'url': bid.url}
            subject = (
                "[codesy] There's $%(ask)d waiting for you!" % email_context
            )
            message = email_template.render(email_context)
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [bid.user.email]
            )
            # use .update to avoid recursive signal processing
            Bid.objects.filter(id=bid.id).update(ask_match_sent=timezone.now())


@receiver(post_save, sender=Bid)
def create_issue_for_bid(sender, instance, **kwargs):
    issue, created = Issue.objects.get_or_create(
        url=instance.url,
        defaults={'state': 'unknown', 'last_fetched': None}
    )
    # use .update to avoid recursive signal processing
    Bid.objects.filter(id=instance.id).update(issue=issue)


class Issue(models.Model):
    url = models.URLField(unique=True, db_index=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    state = models.CharField(max_length=255)
    last_fetched = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        return u'%s (%s)' % (self.url, self.state)


class Claim(models.Model):
    STATUS_CHOICES = (
        ('Submitted', 'Submitted'),
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
        ('Requested', 'Requested'),
        ('Paid', 'Paid')
    )
    issue = models.ForeignKey('Issue')
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
    evidence = models.URLField(blank=False)
    title = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=255,
                              choices=STATUS_CHOICES,
                              default='Submitted')

    objects = ClaimManager()

    class Meta:
        unique_together = (("user", "issue"),)

    def __unicode__(self):
        return u'%s claim on Issue %s (%s)' % (
            self.user, self.issue.id, self.status
        )

    @property
    def ask(self):
        return Bid.objects.get(user=self.user, issue=self.issue).ask

    def save(self, *args, **kwargs):
        is_new = not self.pk
        super(Claim, self).save(*args, **kwargs)
        if is_new:
            # refund any authorize offers by this user
            users_offers = Offer.objects.filter(
                bid__issue=self.issue,
                user=self.user,
                refund_id=u'',
                charge_id__isnull=False
            )
            for offer in users_offers:
                payments.refund(offer)

    def payout(self):
        try:
            # get all authorized offers for this issue
            valid_offers = Offer.objects.filter(
                bid__issue=self.issue,
                charge_id__isnull=False,
                refund_id=u'',
            ).exclude(
                user=self.user
            )
            # check on a surplus
            # TODO: move this to the payments utils
            sum_offers = valid_offers.aggregate(Sum('amount'))['amount__sum']
            users_ask = self.ask
            offer_adjustment = 1
            if sum_offers > users_ask:
                # surplus is the amount of offers over ask
                surplus = sum_offers - users_ask
                # the claim bonus is the claimaints share of the surplus
                claim_bonus = (surplus / (valid_offers.count() + 1))
                # giveback is the aount to distribute among the offerers
                offer_giveback = surplus - claim_bonus
                # this is the percent of the original payout to be charged
                offer_adjustment = 1 - (offer_giveback / sum_offers)

            for offer in valid_offers:
                adjusted_offer_amount = (offer.amount * offer_adjustment)
                discount_amount = offer.amount - adjusted_offer_amount
                payments.refund(offer)
                # create final adjusted offer
                new_offer = Offer(
                    user=offer.user,
                    bid=offer.bid,
                    amount=adjusted_offer_amount,
                    discount=discount_amount
                )
                new_offer.save()
                # capture payment to this users account
                payout = Payout(
                    user=offer.user,
                    claim=self,
                    amount=adjusted_offer_amount,
                    discount=discount_amount
                )
                payout.save()

                payments.charge(offer, payout)
            return True
        except Exception as e:
            print 'payout error: %s' % e.message
            return False

    def payouts(self):
        return Payout.objects.filter(claim=self)

    def successful_payouts(self):
        return Payout.objects.filter(claim=self, api_success=True)

    @property
    def sum_payouts(self):
        return (
            self.successful_payouts()
            .aggregate(Sum('charge_amount'))['charge_amount__sum'])

    @property
    def sum_fees(self):
        sum_of = 0
        for payout in self.successful_payouts():
            sum_of += payout.sum_fees
        return sum_of

    @property
    def sum_credits(self):
        # these are the claim credits not payout credits!
        return self.sum_payouts - self.ask + self.sum_fees

    def votes_by_approval(self, approved):
        return (Vote.objects
                    .filter(claim=self, approved=approved)
                    .exclude(user=self.user))

    def needs_vote_from_user(self, user):
        try:
            Vote.objects.get(claim=self, user=user)
            # User has already voted on this claim
            return False
        except Vote.DoesNotExist:
            user_bid = Bid.objects.filter(issue=self.issue,
                                          user=user,
                                          offer__gt=0)
            if user_bid:
                return True
        return False

    @property
    def num_approvals(self):
        return self.votes_by_approval(True).count()

    @property
    def num_rejections(self):
        return self.votes_by_approval(False).count()

    @property
    def num_votes(self):
        return Vote.objects.filter(claim=self).exclude(user=self.user)

    @property
    def offers(self):
        return (Bid.objects.filter(issue=self.issue)
                           .exclude(user=self.user)
                           .filter(offer__gt=0))

    @property
    def expires(self):
        return self.created + timedelta(days=14)

    def get_absolute_url(self):
        return reverse('claim-status', kwargs={'pk': self.id})


@receiver(post_save, sender=Claim)
def notify_matching_offerers(sender, instance, created, **kwargs):
    # Only notify when the claim is first created
    if not created:
        return True

    email_template = get_template('../templates/email/claim_notify.html')

    current_site = Site.objects.get_current()

    self_Q = models.Q(user=instance.user)
    offered0_Q = models.Q(offer=0)
    others_bids = Bid.objects.filter(
        issue=instance.issue
    ).exclude(
        self_Q | offered0_Q
    )

    for bid in others_bids:
        email_context = ({
            'user': instance.user,
            'url': instance.issue.url,
            'offer': bid.offer,
            'site': current_site,
            'claim_link': instance.get_absolute_url(),
        })
        message = email_template.render(email_context)
        send_mail(
            "[codesy] A claim needs your vote!",
            message,
            settings.DEFAULT_FROM_EMAIL,
            [bid.user.email]
        )


@receiver(post_save, sender=Bid)
@receiver(post_save, sender=Issue)
@receiver(post_save, sender=Claim)
def save_title(sender, instance, **kwargs):
    if isinstance(instance, Claim):
        url = instance.evidence
    else:
        url = instance.url

    try:
        r = requests.get(url)
        title_search = re.search('(?:<title.*>)(.*)(?:<\/title>)', r.text)
        if title_search:
            title = HTMLParser.HTMLParser().unescape(title_search.group(1))
            type(instance).objects.filter(id=instance.id).update(title=title)
    except:
        pass


class Vote(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    claim = models.ForeignKey(Claim)
    # TODO: Vote.approved needs null=True or blank=False
    approved = models.BooleanField(default=None, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("user", "claim"),)

    def __unicode__(self):
        return u'Vote for %s by (%s): %s' % (
            self.claim, self.user, self.approved
        )


@receiver(post_save, sender=Vote)
def update_claim_status(sender, instance, created, **kwargs):
    claim = instance.claim
    offers_needed = claim.offers.count()

    if claim.num_votes > 0:
        claim.status = "Pending"
        claim.save()

    if claim.num_approvals == offers_needed:
        claim.status = "Approved"
        claim.save()

    if offers_needed > 0:
        if (claim.num_rejections / float(offers_needed)) >= 0.5:
            claim.status = "Rejected"
            claim.save()


@receiver(post_save, sender=Vote)
def notify_approved_claim(sender, instance, created, **kwargs):
    claim = instance.claim
    votes_needed = claim.offers.count()

    if claim.num_rejections == votes_needed:
        email_template = get_template('../templates/email/claim_reject.html')

        current_site = Site.objects.get_current()
        email_context = ({
            'url': claim.issue.url,
            'site': current_site,
        })
        message = email_template.render(email_context)
        send_mail(
            "[codesy] Your claim has been rejected",
            message,
            settings.DEFAULT_FROM_EMAIL,
            [claim.user.email]
        )

    if votes_needed == claim.num_approvals:
        email_template = get_template('../templates/email/claim_approved.html')

        current_site = Site.objects.get_current()
        email_context = ({
            'url': claim.issue.url,
            'site': current_site,
        })
        message = email_template.render(email_context)

        send_mail(
            "[codesy] Your claim has been approved",
            message,
            settings.DEFAULT_FROM_EMAIL,
            [claim.user.email]
        )


class Payment(models.Model):
    PROVIDER_CHOICES = (
        ('Stripe', 'Stripe'),
        ('PayPal', 'PayPal'),
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    amount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    transaction_key = models.CharField(
        max_length=255, default=uuid.uuid4, blank=True)
    api_success = models.BooleanField(default=False)
    error_message = models.CharField(max_length=255, blank=True)
    charge_amount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    charge_id = models.CharField(max_length=255, blank=True)
    refund_id = models.CharField(max_length=255, blank=True)
    discount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        is_new = not self.pk
        super(Payment, self).save(*args, **kwargs)
        if is_new:
            self.add_fees()
        super(Payment, self).save(*args, **kwargs)

    def add_fees():
        raise NotImplementedError


class Offer(Payment):
    bid = models.ForeignKey(Bid, related_name='payments')

    provider = models.CharField(
        max_length=255,
        choices=Payment.PROVIDER_CHOICES,
        default='Stripe')

    def __unicode__(self):
        return u'%s Offer payment for bid (%s) paid' % (
            self.bid.user,
            self.bid.id
        )

    @property
    def fees(self):
        return OfferFee.objects.filter(offer=self)

    @property
    def net_offer(self):
        return (self.amount + self.discount)

    @property
    def sum_fees(self):
        return self.fees.aggregate(Sum('amount'))['amount__sum']

    def add_fees(self):
        fee_details = payments.transaction_amounts(self.amount)

        if self.discount:
            discount_fee = OfferCredit(
                offer=self,
                fee_type='surplus',
                amount=self.discount
            )
            discount_fee.save()

        stripe_fee = OfferFee(
            offer=self,
            fee_type='Stripe',
            amount=fee_details['offer_stripe_fee']
        )
        stripe_fee.save()

        codesy_fee = OfferFee(
            offer=self,
            fee_type='codesy',
            amount=fee_details['codesy_fee']
        )
        codesy_fee.save()

        self.charge_amount = fee_details['charge_amount']


class Payout(Payment):
    claim = models.ForeignKey(Claim, related_name='payouts')
    provider = models.CharField(
        max_length=255,
        choices=Payment.PROVIDER_CHOICES,
        default='Stripe')

    def __unicode__(self):
        return u'Payout by %s for claim (%s)' % (
            self.user, self.claim.id
        )

    @property
    def less_discount(self):
        return (self.amount - self.discount)

    def fees(self):
        return PayoutFee.objects.filter(payout=self)

    def credits(self):
        return PayoutCredit.objects.filter(payout=self)

    @property
    def sum_fees(self):
        sum_of = 0
        for fee in self.fees():
            sum_of += fee.amount
        return sum_of

    @property
    def sum_credits(self):
        sum_of = 0
        for credit in self.credits():
            sum_of += credit.amount
        return sum_of

    def add_fees(self):
        fee_details = payments.transaction_amounts(self.amount)

        if self.discount:
            discount_fee = PayoutCredit(
                payout=self,
                fee_type='surplus',
                amount=self.discount
            )
            discount_fee.save()

        stripe_fee = PayoutFee(
            payout=self,
            fee_type='Stripe',
            amount=fee_details['payout_stripe_fee']
        )
        stripe_fee.save()

        codesy_fee = PayoutFee(
            payout=self,
            fee_type='codesy',
            amount=fee_details['codesy_fee']
        )
        codesy_fee.save()

        self.charge_amount = fee_details['payout_amount']


class Fee(models.Model):
    FEE_TYPES = (
        ('PayPal', 'PayPal'),
        ('Stripe', 'Stripe'),
        ('codesy', 'codesy'),
        ('refund', 'refund'),
        ('surplus', 'surplus'),
    )
    fee_type = models.CharField(
        max_length=255,
        choices=FEE_TYPES,
        default='')
    amount = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, default=0)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        abstract = True


class OfferFee(Fee):
    offer = models.ForeignKey(Offer, related_name="offer_fees", null=True)

    def __unicode__(self):
        return "%s fee amount:%s" % (self.fee_type, self.offer)


class OfferCredit(Fee):
    offer = models.ForeignKey(Offer, related_name="offer_credit", null=True)

    def __unicode__(self):
        return "%s credit for %s" % (self.fee_type, self.offer)


class PayoutFee(Fee):
    payout = models.ForeignKey(Payout, related_name="payout_fees", null=True)

    def __unicode__(self):
        return "%s fee for %s" % (self.fee_type, self.payout)


class PayoutCredit(Fee):
    payout = models.ForeignKey(Payout, related_name="payout_credit", null=True)

    def __unicode__(self):
        return "%s credit for %s" % (self.fee_type, self.payout)
