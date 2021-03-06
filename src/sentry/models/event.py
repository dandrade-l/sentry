"""
sentry.models.event
~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2010-2014 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""
from __future__ import absolute_import

import six
import string
import warnings
import pytz

from collections import OrderedDict
from datetime import datetime
from dateutil.parser import parse as parse_date
from django.db import models
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from hashlib import md5

from semaphore.processing import StoreNormalizer

from sentry import eventtypes, options
from sentry.constants import EVENT_ORDERING_KEY
from sentry.db.models import (
    BoundedBigIntegerField,
    BoundedIntegerField,
    Model,
    NodeData,
    NodeField,
    sane_repr
)
from sentry.db.models.manager import EventManager
from sentry.interfaces.base import get_interfaces
from sentry.utils import metrics
from sentry.utils.cache import memoize
from sentry.utils.canonical import CanonicalKeyDict, CanonicalKeyView
from sentry.utils.safe import get_path
from sentry.utils.strings import truncatechars
from sentry.utils.sdk import configure_scope


def _should_skip_to_python(event_data):
    event_id = event_data.get("event_id")
    if not event_id:
        return False

    sample_rate = options.get('store.empty-interface-sample-rate')
    if sample_rate == 0:
        return False

    return int(md5(event_id).hexdigest(), 16) % (10 ** 8) <= (sample_rate * (10 ** 8))


class EventDict(CanonicalKeyDict):
    def __init__(self, data, **kwargs):
        rust_renormalized = _should_skip_to_python(data)
        if rust_renormalized:
            normalizer = StoreNormalizer(is_renormalize=True)
            data = normalizer.normalize_event(dict(data))

        metrics.incr('rust.renormalized',
                     tags={'value': rust_renormalized})

        with configure_scope() as scope:
            scope.set_tag("rust.renormalized", rust_renormalized)

        CanonicalKeyDict.__init__(self, data, **kwargs)


class EventCommon(object):
    @classmethod
    def generate_node_id(cls, project_id, event_id):
        """
        Returns a deterministic node_id for this event based on the project_id
        and event_id which together are globally unique. The event body should
        be saved under this key in nodestore so it can be retrieved using the
        same generated id when we only have project_id and event_id.
        """
        return md5('{}:{}'.format(project_id, event_id)).hexdigest()

    # TODO (alex) We need a better way to cache these properties. functools32
    # doesn't quite do the trick as there is a reference bug with unsaved
    # models. But the current _group_cache thing is also clunky because these
    # properties need to be stripped out in __getstate__.
    @property
    def group(self):
        from sentry.models import Group
        if not hasattr(self, '_group_cache'):
            self._group_cache = Group.objects.get(id=self.group_id)
        return self._group_cache

    @group.setter
    def group(self, group):
        self.group_id = group.id
        self._group_cache = group

    @property
    def project(self):
        from sentry.models import Project
        if not hasattr(self, '_project_cache'):
            self._project_cache = Project.objects.get(id=self.project_id)
        return self._project_cache

    @project.setter
    def project(self, project):
        self.project_id = project.id
        self._project_cache = project

    def get_interfaces(self):
        was_renormalized = _should_skip_to_python(self.data)

        return CanonicalKeyView(get_interfaces(self.data, rust_renormalized=was_renormalized))

    @memoize
    def interfaces(self):
        return self.get_interfaces()

    def get_interface(self, name):
        return self.interfaces.get(name)

    def get_legacy_message(self):
        # TODO(mitsuhiko): remove this code once it's unused.  It's still
        # being used by plugin code and once the message rename is through
        # plugins should instead swithc to the actual message attribute or
        # this method could return what currently is real_message.
        return get_path(self.data, 'logentry', 'formatted') \
            or get_path(self.data, 'logentry', 'message') \
            or self.message

    def get_event_type(self):
        """
        Return the type of this event.

        See ``sentry.eventtypes``.
        """
        return self.data.get('type', 'default')

    def get_event_metadata(self):
        """
        Return the metadata of this event.

        See ``sentry.eventtypes``.
        """
        # For some inexplicable reason we have some cases where the data
        # is completely empty.  In that case we want to hobble along
        # further.
        return self.data.get('metadata') or {}

    def get_hashes(self):
        """
        Returns the calculated hashes for the event.  This uses the stored
        information if available.  Grouping hashes will take into account
        fingerprinting and checksums.
        """
        # If we have hashes stored in the data we use them, otherwise we
        # fall back to generating new ones from the data
        hashes = self.data.get('hashes')
        if hashes is not None:
            return hashes
        return filter(None, [x.get_hash() for x in self.get_grouping_variants().values()])

    def get_grouping_variants(self, force_config=None):
        """
        This is similar to `get_hashes` but will instead return the
        grouping components for each variant in a dictionary.
        """
        from sentry.grouping.api import get_grouping_variants_for_event
        return get_grouping_variants_for_event(self, config_name=force_config)

    def get_primary_hash(self):
        # TODO: This *might* need to be protected from an IndexError?
        return self.get_hashes()[0]

    @property
    def title(self):
        # also see event_manager.py which inserts this for snuba
        et = eventtypes.get(self.get_event_type())()
        return et.get_title(self.get_event_metadata())

    @property
    def culprit(self):
        # For a while events did not save the culprit
        return self.data.get('culprit') or self.group.culprit

    @property
    def location(self):
        # also see event_manager.py which inserts this for snuba
        et = eventtypes.get(self.get_event_type())()
        return et.get_location(self.get_event_metadata())

    @property
    def real_message(self):
        # XXX(mitsuhiko): this is a transitional attribute that should be
        # removed.  `message` will be renamed to `search_message` and this
        # will become `message`.
        return get_path(self.data, 'logentry', 'formatted') \
            or get_path(self.data, 'logentry', 'message') \
            or ''

    @property
    def organization(self):
        return self.project.organization

    @property
    def version(self):
        return self.data.get('version', '5')

    @property
    def ip_address(self):
        ip_address = get_path(self.data, 'user', 'ip_address')
        if ip_address:
            return ip_address

        remote_addr = get_path(self.data, 'request', 'env', 'REMOTE_ADDR')
        if remote_addr:
            return remote_addr

        return None

    @property
    def tags(self):
        try:
            rv = sorted([(t, v) for t, v in get_path(
                self.data, 'tags', filter=True) or () if t is not None and v is not None])
            return rv
        except ValueError:
            # at one point Sentry allowed invalid tag sets such as (foo, bar)
            # vs ((tag, foo), (tag, bar))
            return []

    # For compatibility, still used by plugins.
    def get_tags(self):
        return self.tags

    def get_tag(self, key):
        for t, v in self.get_tags():
            if t == key:
                return v
        return None

    @property
    def release(self):
        return self.get_tag('sentry:release')

    @property
    def dist(self):
        return self.get_tag('sentry:dist')

    def get_raw_data(self):
        """Returns the internal raw event data dict."""
        return dict(self.data.items())

    @property
    def size(self):
        data_len = 0
        for value in six.itervalues(self.data):
            data_len += len(repr(value))
        return data_len

    @property
    def transaction(self):
        return self.get_tag('transaction')

    def get_email_subject(self):
        template = self.project.get_option('mail:subject_template')
        if template:
            template = EventSubjectTemplate(template)
        else:
            template = DEFAULT_SUBJECT_TEMPLATE
        return truncatechars(
            template.safe_substitute(
                EventSubjectTemplateData(self),
            ),
            128,
        ).encode('utf-8')

    def get_environment(self):
        from sentry.models import Environment

        if not hasattr(self, '_environment_cache'):
            self._environment_cache = Environment.objects.get(
                organization_id=self.project.organization_id,
                name=Environment.get_name_or_default(self.get_tag('environment')),
            )

        return self._environment_cache

    def as_dict(self):
        """Returns the data in normalized form for external consumers."""
        # We use a OrderedDict to keep elements ordered for a potential JSON serializer
        data = OrderedDict()
        data['event_id'] = self.event_id
        data['project'] = self.project_id
        data['release'] = self.release
        data['dist'] = self.dist
        data['platform'] = self.platform
        data['message'] = self.real_message
        data['datetime'] = self.datetime
        data['time_spent'] = self.time_spent
        data['tags'] = [(k.split('sentry:', 1)[-1], v) for (k, v) in self.tags]
        for k, v in sorted(six.iteritems(self.data)):
            if k in data:
                continue
            if k == 'sdk':
                v = {v_k: v_v for v_k, v_v in six.iteritems(v) if v_k != 'client_ip'}
            data[k] = v

        # for a long time culprit was not persisted.  In those cases put
        # the culprit in from the group.
        if data.get('culprit') is None:
            data['culprit'] = self.group.culprit

        # Override title and location with dynamically generated data
        data['title'] = self.title
        data['location'] = self.location

        return data

    # ============================================
    # DEPRECATED
    # ============================================

    @property
    def level(self):
        # we might want to move to this:
        # return LOG_LEVELS_MAP.get(self.get_level_display()) or self.group.level
        return self.group.level

    def get_level_display(self):
        # we might want to move to this:
        # return self.get_tag('level') or self.group.get_level_display()
        return self.group.get_level_display()

    # deprecated accessors

    @property
    def logger(self):
        warnings.warn('Event.logger is deprecated. Use Event.tags instead.', DeprecationWarning)
        return self.get_tag('logger')

    @property
    def site(self):
        warnings.warn('Event.site is deprecated. Use Event.tags instead.', DeprecationWarning)
        return self.get_tag('site')

    @property
    def server_name(self):
        warnings.warn(
            'Event.server_name is deprecated. Use Event.tags instead.',
            DeprecationWarning)
        return self.get_tag('server_name')

    @property
    def checksum(self):
        warnings.warn('Event.checksum is no longer used', DeprecationWarning)
        return ''

    def error(self):  # TODO why is this not a property?
        warnings.warn('Event.error is deprecated, use Event.title', DeprecationWarning)
        return self.title

    error.short_description = _('error')

    @property
    def message_short(self):
        warnings.warn('Event.message_short is deprecated, use Event.title', DeprecationWarning)
        return self.title


class SnubaEvent(EventCommon):
    """
        An event backed by data stored in snuba.

        This is a readonly event and does not support event creation or save.
        The basic event data is fetched from snuba, and the event body is
        fetched from nodestore and bound to the data property in the same way
        as a regular Event.
    """

    # The list of columns that we should request from snuba to be able to fill
    # out the object.
    selected_columns = [
        'event_id',
        'project_id',
        'message',
        'title',
        'type',
        'location',
        'culprit',
        'timestamp',
        'group_id',
        'platform',

        # Required to provide snuba-only tags
        'tags.key',
        'tags.value',

        # Required to provide snuba-only 'user' interface
        'user_id',
        'username',
        'ip_address',
        'email',
    ]

    __repr__ = sane_repr('project_id', 'group_id')

    @classmethod
    def get_event(cls, project_id, event_id):
        from sentry.utils import snuba
        result = snuba.raw_query(
            start=datetime.utcfromtimestamp(0),  # will be clamped to project retention
            end=datetime.utcnow(),  # will be clamped to project retention
            selected_columns=cls.selected_columns,
            filter_keys={
                'event_id': [event_id],
                'project_id': [project_id],
            },
        )
        if 'error' not in result and len(result['data']) == 1:
            return SnubaEvent(result['data'][0])
        return None

    def __init__(self, snuba_values):
        assert set(snuba_values.keys()) == set(self.selected_columns)

        self.__dict__ = snuba_values

        # This should be lazy loaded and will only be accessed if we access any
        # properties on self.data
        node_id = SnubaEvent.generate_node_id(self.project_id, self.event_id)
        self.data = NodeData(None, node_id, data=None)

    # ============================================
    # Snuba-only implementations of properties that
    # would otherwise require nodestore data.
    # ============================================
    @property
    def tags(self):
        """
        Override of tags property that uses tags from snuba rather than
        the nodestore event body. This might be useful for implementing
        tag deletions without having to rewrite nodestore blobs.
        """
        keys = getattr(self, 'tags.key', None)
        values = getattr(self, 'tags.value', None)
        if keys and values and len(keys) == len(values):
            return sorted(zip(keys, values))
        return []

    def get_interface(self, name):
        """
        Override of interface getter that lets us return some interfaces
        directly from Snuba data.
        """
        if name in ['user']:
            from sentry.interfaces.user import User
            # This is a fake version of the User interface constructed
            # from just the data we have in Snuba.
            snuba_user = {
                'id': self.user_id,
                'email': self.email,
                'username': self.username,
                'ip_address': self.ip_address,
            }
            if any(v is not None for v in snuba_user.values()):
                return User.to_python(snuba_user)
        return self.interfaces.get(name)

    def get_event_type(self):
        return self.__dict__.get('type', 'default')

    # These should all have been normalized to the correct values on
    # the way in to snuba, so we should be able to just use them as is.
    @property
    def ip_address(self):
        return self.__dict__['ip_address']

    @property
    def title(self):
        return self.__dict__['title']

    @property
    def culprit(self):
        return self.__dict__['culprit']

    @property
    def location(self):
        return self.__dict__['location']

    # ============================================
    # Snuba implementations of django Fields
    # ============================================
    @property
    def datetime(self):
        """
        Reconstruct the datetime of this event from the snuba timestamp
        """
        # dateutil seems to use tzlocal() instead of UTC even though the string
        # ends with '+00:00', so just replace the TZ with UTC because we know
        # all timestamps from snuba are UTC.
        return parse_date(self.timestamp).replace(tzinfo=pytz.utc)

    @property
    def time_spent(self):
        return None

    @property
    def id(self):
        # Because a snuba event will never have a django row id, just return
        # the hex event_id here. We should be moving to a world where we never
        # have to reference the row id anyway.
        return self.event_id

    @property
    def next_event(self):
        return None

    @property
    def prev_event(self):
        return None

    def save(self):
        raise NotImplementedError


class Event(EventCommon, Model):
    """
    An event backed by data stored in postgres.

    """
    __core__ = False

    group_id = BoundedBigIntegerField(blank=True, null=True)
    event_id = models.CharField(max_length=32, null=True, db_column="message_id")
    project_id = BoundedBigIntegerField(blank=True, null=True)
    message = models.TextField()
    platform = models.CharField(max_length=64, null=True)
    datetime = models.DateTimeField(default=timezone.now, db_index=True)
    time_spent = BoundedIntegerField(null=True)
    data = NodeField(
        blank=True,
        null=True,
        ref_func=lambda x: x.project_id or x.project.id,
        ref_version=2,
        wrapper=EventDict,
    )

    objects = EventManager()

    class Meta:
        app_label = 'sentry'
        db_table = 'sentry_message'
        verbose_name = _('message')
        verbose_name_plural = _('messages')
        unique_together = (('project_id', 'event_id'), )
        index_together = (('group_id', 'datetime'), )

    __repr__ = sane_repr('project_id', 'group_id')

    def __getstate__(self):
        state = Model.__getstate__(self)

        # do not pickle cached info.  We want to fetch this on demand
        # again.  In particular if we were to pickle interfaces we would
        # pickle a CanonicalKeyView which old sentry workers do not know
        # about
        state.pop('_project_cache', None)
        state.pop('_group_cache', None)
        state.pop('interfaces', None)

        return state

    # Find next and previous events based on datetime and id. We cannot
    # simply `ORDER BY (datetime, id)` as this is too slow (no index), so
    # we grab the next 5 / prev 5 events by datetime, and sort locally to
    # get the next/prev events. Given that timestamps only have 1-second
    # granularity, this will be inaccurate if there are more than 5 events
    # in a given second.
    @property
    def next_event(self):
        events = self.__class__.objects.filter(
            datetime__gte=self.datetime,
            group_id=self.group_id,
        ).exclude(id=self.id).order_by('datetime')[0:5]

        events = [e for e in events if e.datetime == self.datetime and e.id > self.id or
                  e.datetime > self.datetime]
        events.sort(key=EVENT_ORDERING_KEY)
        return events[0] if events else None

    @property
    def prev_event(self):
        events = self.__class__.objects.filter(
            datetime__lte=self.datetime,
            group_id=self.group_id,
        ).exclude(id=self.id).order_by('-datetime')[0:5]

        events = [e for e in events if e.datetime == self.datetime and e.id < self.id or
                  e.datetime < self.datetime]
        events.sort(key=EVENT_ORDERING_KEY, reverse=True)
        return events[0] if events else None


class EventSubjectTemplate(string.Template):
    idpattern = r'(tag:)?[_a-z][_a-z0-9]*'


class EventSubjectTemplateData(object):
    tag_aliases = {
        'release': 'sentry:release',
        'dist': 'sentry:dist',
        'user': 'sentry:user',
    }

    def __init__(self, event):
        self.event = event

    def __getitem__(self, name):
        if name.startswith('tag:'):
            name = name[4:]
            value = self.event.get_tag(self.tag_aliases.get(name, name))
            if value is None:
                raise KeyError
            return six.text_type(value)
        elif name == 'project':
            return self.event.project.get_full_name()
        elif name == 'projectID':
            return self.event.project.slug
        elif name == 'shortID':
            return self.event.group.qualified_short_id
        elif name == 'orgID':
            return self.event.organization.slug
        elif name == 'title':
            return self.event.title
        raise KeyError


DEFAULT_SUBJECT_TEMPLATE = EventSubjectTemplate('$shortID - $title')
