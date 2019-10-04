import re
import json
from io import BytesIO
import functools
import collections
from urllib.parse import urlparse, parse_qs

from tornado.httpclient import HTTPResponse
from tornado.httputil import HTTPHeaders
from pytest import fixture, mark

from ..gitlab import GitLabOAuthenticator, GITLAB_API_VERSION

from .mocks import setup_oauth_mock

API_ENDPOINT = '/api/v%s' % (GITLAB_API_VERSION,)


def user_model(username, id=1, is_admin=False):
    """Return a user model"""
    user = {
        'username': username,
        'id': id,
    }
    if is_admin:
      # Some versions of the API do not return the is_admin property
        # for non-admin users (See #115).
        user['is_admin'] = True
    return user

@fixture
def gitlab_client(client):
    setup_oauth_mock(client,
        host='gitlab.com',
        access_token_path='/oauth/token',
        user_path=API_ENDPOINT + '/user',
    )
    return client


async def test_gitlab(gitlab_client):
    authenticator = GitLabOAuthenticator()
    handler = gitlab_client.handler_for_user(user_model('wash'))
    user_info = await authenticator.authenticate(handler)
    assert sorted(user_info) == ['auth_state', 'name']
    name = user_info['name']
    assert name == 'wash'
    auth_state = user_info['auth_state']
    assert 'access_token' in auth_state
    assert 'gitlab_user' in auth_state


def make_link_header(urlinfo, page):
    return {'Link': '<{}://{}{}?page={}>;rel="next"'
                    .format(urlinfo.scheme, urlinfo.netloc, urlinfo.path, page)}


async def test_group_whitelist(gitlab_client):
    client = gitlab_client
    authenticator = GitLabOAuthenticator()

    ## set up fake Gitlab API

    user_groups = collections.OrderedDict({
        'grif': ['red', 'yellow'],
        'simmons': ['red', 'yellow'],
        'caboose': ['blue', 'yellow'],
        'burns': ['blue', 'yellow'],
    })

    def group_user_model(username, is_admin=False):
        return user_model(username,
                          list(user_groups.keys()).index(username) + 1,
                          is_admin)


    group_regex = re.compile(API_ENDPOINT + r'/groups/(.*)/members/all')
    uname_regex = re.compile('query=(.*)')
    def is_member(request):
        urlinfo = urlparse(request.url)
        group = group_regex.match(urlinfo.path).group(1)
        uname = uname_regex.match(urlinfo.query).group(1)
        if group in user_groups[uname]:
            return HTTPResponse(request, 200)
        else:
            return HTTPResponse(request, 404)

    def groups(paginate, request):
        urlinfo = urlparse(request.url)
        _, token = request._headers.get('Authorization').split()
        user = client.access_tokens[token]['username']
        if not paginate:
            return [{'path': group} for group in user_groups[user]]
        else:
            page = parse_qs(urlinfo.query).get('page', ['1'])
            page = int(page[0])
            return groups_paginated(user, page, urlinfo,
                                    functools.partial(HTTPResponse, request))

    def groups_paginated(user, page, urlinfo, response):
        if page < len(user_groups[user]):
            headers = make_link_header(urlinfo, page + 1)
        elif page == len(user_groups[user]):
            headers = {}
        else:
            return response(400)

        headers.update({'Content-Type': 'application/json'})

        ret = [{'path': user_groups[user][page - 1]}]

        return response(200, headers=HTTPHeaders(headers),
                        buffer=BytesIO(json.dumps(ret).encode('utf-8')))

    client.hosts['gitlab.com'].append(
        (group_regex, is_member)
    )

    ## actual tests

    for paginate in (False, True):
        client.hosts['gitlab.com'].append(
            (API_ENDPOINT + '/groups', functools.partial(groups, paginate))
        )

        authenticator.gitlab_group_whitelist = ['blue']

        handler = client.handler_for_user(group_user_model('caboose'))
        user_info = await authenticator.authenticate(handler)
        name = user_info['name']
        assert name == 'caboose'

        handler = client.handler_for_user(group_user_model('burns', is_admin=True))
        user_info = await authenticator.authenticate(handler)
        name = user_info['name']
        assert name == 'burns'

        handler = client.handler_for_user(group_user_model('grif'))
        name = await authenticator.authenticate(handler)
        assert name is None

        handler = client.handler_for_user(group_user_model('simmons', is_admin=True))
        name = await authenticator.authenticate(handler)
        assert name is None

        # reverse it, just to be safe
        authenticator.gitlab_group_whitelist = ['red']

        handler = client.handler_for_user(group_user_model('caboose'))
        name = await authenticator.authenticate(handler)
        assert name is None

        handler = client.handler_for_user(group_user_model('grif'))
        user_info = await authenticator.authenticate(handler)
        name = user_info['name']
        assert name == 'grif'

        client.hosts['gitlab.com'].pop()


async def test_project_id_whitelist(gitlab_client):
    client = gitlab_client
    authenticator = GitLabOAuthenticator()

    user_projects = {
        '1231231': {
            'john': {
                'id': 3588673,
                'name': 'john',
                'username': 'john',
                'state': 'active',
                'avatar_url': 'https://secure.gravatar.com/avatar/382a6b306679b2d97b547bfff3d73242?s=80&d=identicon',
                'web_url': 'https://gitlab.com/john',
                'access_level': 10,  # Guest
                'expires_at': '2030-02-23'
            },
            'harry': {
                'id': 3588674,
                'name': 'harry',
                'username': 'harry',
                'state': 'active',
                'avatar_url': 'https://secure.gravatar.com/avatar/382a6b306679b2d97b547bfff3d73242?s=80&d=identicon',
                'web_url': 'https://gitlab.com/harry',
                'access_level': 30,  # Developer
                'expires_at': '2030-02-23'
            }
        }
    }
    john_user_model = user_model('john', 3588673)
    harry_user_model = user_model('harry', 3588674)
    sheila_user_model = user_model('sheila', 3588675)

    project_regex = re.compile(API_ENDPOINT + r'/projects/(.*)/members/all')
    uname_regex = re.compile('query=(.*)')

    def is_member(request):
        urlinfo = urlparse(request.url)
        project_id = project_regex.match(urlinfo.path).group(1)
        uname = uname_regex.match(urlinfo.query).group(1)

        if user_projects.get(project_id) and user_projects.get(project_id).get(uname):
            res = user_projects.get(project_id).get(uname)
            return HTTPResponse(request=request, code=200,
                buffer=BytesIO(json.dumps(res).encode('utf8')),
                headers={'Content-Type': 'application/json'},
            )
        else:
            return HTTPResponse(request=request, code=404,
                buffer=BytesIO(''.encode('utf8'))
            )

    client.hosts['gitlab.com'].append(
        (project_regex, is_member)
    )

    authenticator.gitlab_project_id_whitelist = [1231231]

    # Forbidden since John has guest access
    handler = client.handler_for_user(john_user_model)
    user_info = await authenticator.authenticate(handler)
    assert user_info is None

    # Authenticated since Harry has developer access to the project
    handler = client.handler_for_user(harry_user_model)
    user_info = await authenticator.authenticate(handler)
    name = user_info['name']
    assert name == 'harry'

    # Forbidden since Sheila doesn't have access to the project
    handler = client.handler_for_user(sheila_user_model)
    user_info = await authenticator.authenticate(handler)
    assert user_info is None

    authenticator.gitlab_project_id_whitelist = [123123152543]

    # Forbidden since the project does not exist.
    handler = client.handler_for_user(harry_user_model)
    user_info = await authenticator.authenticate(handler)
    assert user_info is None

    authenticator.gitlab_project_id_whitelist = [123123152543, 1231231]

    # Authenticated since Harry has developer access to one of the project in the list
    handler = client.handler_for_user(harry_user_model)
    user_info = await authenticator.authenticate(handler)
    name = user_info['name']
    assert name == 'harry'
