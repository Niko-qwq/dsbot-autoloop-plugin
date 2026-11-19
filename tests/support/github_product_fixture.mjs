import assert from 'node:assert/strict';
import { dispatchGitHubProduct } from '../../runtime/github_product.mjs';

const repository = {
  id: 287, owner: 'owner', name: 'workflow', fullName: 'owner/workflow', defaultBranch: 'main',
  visibility: 'private', installationId: 17,
};

function clientFor(dispatch) {
  const calls = [];
  const client = {
    async inspect() { return { appId: 23, slug: 'autoloop' }; },
    async requestRepository(request) {
      assert.deepEqual(request.repository, repository);
      calls.push(request);
      return dispatch(request);
    },
  };
  return { client, calls, run: (operation, args = {}) => dispatchGitHubProduct(client, { operation, repository, ...args }) };
}

function result(data, link = null) {
  return { data, link, etag: null, status: 200 };
}

function notFound() {
  const error = new Error('Not found');
  error.status = 404;
  return error;
}

export { repository, clientFor, result, notFound };
