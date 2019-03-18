from __future__ import absolute_import, print_function

from sentry.models import PlatformExternalIssue
from sentry.testutils import APITestCase
from sentry.testutils.helpers import with_feature


class GroupExternalIssueDetailsEndpointTest(APITestCase):
    def setUp(self):
        self.login_as(user=self.user)

        self.group = self.create_group()
        self.external_issue = PlatformExternalIssue.objects.create(
            group_id=self.group.id,
            service_type='sentry-app',
            display_name='App#issue-1',
            web_url='https://example.com/app/issues/1',
        )

        self.url = u'/api/0/issues/{}/external-issues/{}/'.format(
            self.group.id,
            self.external_issue.id,
        )

    @with_feature('organizations:sentry-apps')
    def test_deletes_external_issue(self):
        response = self.client.delete(self.url, format='json')

        assert response.status_code == 204, response.content
        assert not PlatformExternalIssue.objects.filter(
            id=self.external_issue.id,
        ).exists()

    @with_feature('organizations:sentry-apps')
    def test_handles_non_existing_external_issue(self):
        url = u'/api/0/issues/{}/external-issues/{}/'.format(
            self.group.id,
            99999,
        )

        response = self.client.delete(url, format='json')

        assert response.status_code == 404, response.content
