import handleXhrErrorResponse from 'app/utils/handleXhrErrorResponse';

export function fetchSavedSearches(api, orgId, projectId = null) {
  const url = projectId
    ? `/projects/${orgId}/${projectId}/searches/`
    : `/organizations/${orgId}/searches/`;
  return api.requestPromise(url, {
    method: 'GET',
  });
}

const getRecentSearchUrl = orgId => `/organizations/${orgId}/recent-searches/`;

/**
 * Saves search term for `user` + `orgId`
 *
 * @param {Object} api API client
 * @param {String} orgId Organization slug
 * @param {Number} type Context for where search happened, 0 for issue, 1 for event
 * @param {String} query The search term that was used
 */
export function saveRecentSearch(api, orgId, type, query) {
  const url = getRecentSearchUrl(orgId);
  const promise = api
    .requestPromise(url, {
      method: 'POST',
      query: {
        query,
        type,
      },
    })
    .catch(handleXhrErrorResponse('Unable to save a recent search'));

  return promise;
}

/**
 * Fetches a list of recent search terms conducted by `user` for `orgId`
 *
 * @param {Object} api API client
 * @param {String} orgId Organization slug
 * @param {Number} type Context for where search happened, 0 for issue, 1 for event
 * @param {String} query A query term used to filter results
 *
 * @return {Object[]} Returns a list of objects of recent search queries performed by user
 */
export function fetchRecentSearches(api, orgId, type, query) {
  const url = getRecentSearchUrl(orgId);
  const promise = api
    .requestPromise(url, {
      query: {
        query,
        type,
      },
    })
    .catch(handleXhrErrorResponse('Unable to fetch recent searches'));

  return promise;
}
