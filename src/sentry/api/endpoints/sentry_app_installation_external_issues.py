from __future__ import absolute_import

from rest_framework.response import Response

from sentry import features
from sentry.api.bases import SentryAppInstallationBaseEndpoint
from sentry.api.serializers import serialize
from sentry.mediators.external_issues import IssueLinkCreator
from sentry.models import Group, Project


class SentryAppInstallationExternalIssuesEndpoint(SentryAppInstallationBaseEndpoint):
    def post(self, request, installation):
        if not features.has('organizations:sentry-apps',
                            installation.organization,
                            actor=request.user):
            return Response(status=404)

        data = request.DATA.copy()

        if not set(['groupId', 'action', 'uri']).issubset(data.keys()):
            return Response(status=400)

        group_id = data.get('groupId')
        del data['groupId']

        try:
            group = Group.objects.get(
                id=group_id,
                project_id__in=Project.objects.filter(
                    organization_id=installation.organization_id,
                )
            )
        except Group.DoesNotExist:
            return Response(status=404)

        action = data['action']
        del data['action']

        uri = data.get('uri')
        del data['uri']

        try:
            external_issue = IssueLinkCreator.run(
                install=installation,
                group=group,
                action=action,
                fields=data,
                uri=uri,
            )
        except Exception:
            return Response({'error': 'Error communicating with Sentry App service'}, status=400)

        return Response(serialize(external_issue))
