from datetime import timedelta

from modelcluster.models import ClusterableModel
from modelcluster.fields import ParentalKey
from django.db import models
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils.dateparse import parse_duration
from django.utils import timezone

from drip.utils import get_user_model
from wagtail.core.models import Orderable
from wagtail.admin.edit_handlers import FieldPanel, InlinePanel


class Drip(ClusterableModel):
    date = models.DateTimeField(auto_now_add=True)
    last_changed = models.DateTimeField(auto_now=True)

    name = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Drip Name',
        help_text='A unique name for this drip.')

    enabled = models.BooleanField(default=False)

    from_email = models.EmailField(null=True, blank=True,
        help_text='Set a custom from email.')
    from_email_name = models.CharField(max_length=150, null=True, blank=True,
        help_text="Set a name for a custom from email.")
    reply_to = models.EmailField(null=True, blank=True,
        help_text='Set a custom reply-to email.')
    subject_template = models.TextField(null=True, blank=True)
    body_html_template = models.TextField(null=True, blank=True,
        help_text='You will have settings and user in the context. For example {{ user.first_name }} {{ user.last_name }} will return "Joe Smith", and {{ settings.BASE_URL }} will return your domain name.')
    message_class = models.CharField(max_length=120, blank=True, default='default')

    panels = [
        FieldPanel('name'),
        FieldPanel('enabled'),
        FieldPanel('from_email'),
        FieldPanel('from_email_name'),
        FieldPanel('subject_template'),
        FieldPanel('body_html_template'),
        InlinePanel('queryset_rules'),
    ]

    @property
    def drip(self):
        from drip.drips import DripBase

        drip = DripBase(drip_model=self,
                        name=self.name,
                        from_email=self.from_email if self.from_email else None,
                        from_email_name=self.from_email_name if self.from_email_name else None,
                        reply_to=self.reply_to if self.reply_to else None,
                        subject_template=self.subject_template if self.subject_template else None,
                        body_template=self.body_html_template if self.body_html_template else None)
        return drip

    def __unicode__(self):
        return self.name


class SentDrip(models.Model):
    """
    Keeps a record of all sent drips.
    """
    date = models.DateTimeField(auto_now_add=True)

    drip = models.ForeignKey(Drip, related_name='sent_drips', on_delete=models.CASCADE)
    user = models.ForeignKey(getattr(settings, 'AUTH_USER_MODEL', 'auth.User'), related_name='sent_drips', on_delete=models.CASCADE)

    subject = models.TextField()
    body = models.TextField()
    from_email = models.EmailField(null=True, default=None)
    from_email_name = models.CharField(max_length=150, null=True, default=None)
    reply_to = models.EmailField(null=True, default=None)
    name = models.CharField(max_length=255, null=True, default=None)

METHOD_TYPES = (
    ('filter', 'Filter'),
    ('exclude', 'Exclude'),
)

LOOKUP_TYPES = (
    ('exact', 'exactly'),
    ('iexact', 'exactly (case insensitive)'),
    ('contains', 'contains'),
    ('icontains', 'contains (case insensitive)'),
    ('regex', 'regex'),
    ('iregex', 'regex (case insensitive)'),
    ('gt', 'greater than'),
    ('gte', 'greater than or equal to'),
    ('lt', 'less than'),
    ('lte', 'less than or equal to'),
    ('startswith', 'starts with'),
    ('istartswith', 'starts with (case insensitive)'),
    ('endswith', 'ends with'),
    ('iendswith', 'ends with (case insensitive)'),
)

class QuerySetRule(Orderable):
    date = models.DateTimeField(auto_now_add=True)
    last_changed = models.DateTimeField(auto_now=True)

    drip = ParentalKey(Drip, related_name='queryset_rules', on_delete=models.CASCADE)

    method_type = models.CharField(max_length=12, default='filter', choices=METHOD_TYPES)
    field_name = models.CharField(max_length=128, verbose_name='User Field')
    lookup_type = models.CharField(max_length=12, default='exact', choices=LOOKUP_TYPES)

    field_value = models.CharField(max_length=255,
        help_text=('Can be anything from a number, to a string. Or, do ' +
                   '`now-7 days` or `today+3 days` for fancy timedelta.'))

    panels = [
        FieldPanel('method_type'),
        FieldPanel('field_name'),
        FieldPanel('lookup_type'),
        FieldPanel('field_value'),
    ]

    def clean(self):
        User = get_user_model()
        try:
            self.apply(User.objects.all())
        except Exception as e:
            raise ValidationError(
                '%s raised trying to apply rule: %s' % (type(e).__name__, e))

    @property
    def annotated_field_name(self):
        field_name = self.field_name
        if field_name.endswith('__count'):
            agg, _, _ = field_name.rpartition('__')
            field_name = 'num_%s' % agg.replace('__', '_')

        return field_name

    def apply_any_annotation(self, qs):
        if self.field_name.endswith('__count'):
            field_name = self.annotated_field_name
            agg, _, _ = self.field_name.rpartition('__')
            qs = qs.annotate(**{field_name: models.Count(agg, distinct=True)})
        return qs

    def parse_duration(self, value):
        value = value.lstrip('+')
        duration = parse_duration(value)
        if duration is None:
            if not ',' in value:
                # django parse_duration requires 'x days, S'
                duration = parse_duration(value + ', 0')
                if duration is None:
                    raise ValueError("Could not parse %s" % value)
        return duration

    def filter_kwargs(self, qs, now=timezone.now):
        # Support Count() as m2m__count
        field_name = self.annotated_field_name
        field_name = '__'.join([field_name, self.lookup_type])
        field_value = self.field_value

        # set time deltas and dates
        if self.field_value.startswith('now'):
            field_value = self.field_value.replace('now', '')
            field_value = now() + self.parse_duration(field_value)
        elif self.field_value.startswith('today'):
            field_value = self.field_value.replace('today', '')
            field_value = now().date() + self.parse_duration(field_value)

        # F expressions
        if self.field_value.startswith('F_'):
            field_value = self.field_value.replace('F_', '')
            field_value = models.F(field_value)

        # set booleans
        if self.field_value == 'True':
            field_value = True
        if self.field_value == 'False':
            field_value = False

        kwargs = {field_name: field_value}

        return kwargs

    def apply(self, qs, now=timezone.now):

        kwargs = self.filter_kwargs(qs, now)
        qs = self.apply_any_annotation(qs)

        if self.method_type == 'filter':
            return qs.filter(**kwargs)
        elif self.method_type == 'exclude':
            return qs.exclude(**kwargs)

        # catch as default
        return qs.filter(**kwargs)
