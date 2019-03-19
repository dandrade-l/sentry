from __future__ import absolute_import

from django.db import models
from django.utils import timezone

from sentry.db.models import FlexibleForeignKey, Model, sane_repr


class RecentSearch(Model):
    """
    A saved search query.
    """
    __core__ = True

    organization = FlexibleForeignKey('sentry.Organization')
    user = FlexibleForeignKey('sentry.User', db_index=False)
    type = models.PositiveSmallIntegerField()
    query = models.TextField()
    last_seen = models.DateTimeField(default=timezone.now)
    date_added = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'sentry'
        db_table = 'sentry_recentsearch'
        unique_together = (('user', 'organization', 'type', 'query'),)

    __repr__ = sane_repr('organization_id', 'user_id', 'type', 'query')
