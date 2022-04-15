# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import logging
from datetime import datetime
from gzip import GzipFile
from hashlib import sha256
from http.client import BAD_REQUEST, FORBIDDEN, INTERNAL_SERVER_ERROR, METHOD_NOT_ALLOWED, NOT_FOUND, UNAUTHORIZED
from io import StringIO
from traceback import format_exc

# Django
from django.http import QueryDict

# regex
from regex import compile as regex_compile

# Zato
from zato.common.api import CHANNEL, DATA_FORMAT, HL7, HTTP_SOAP, MISC, RATE_LIMIT, SEC_DEF_TYPE, SIMPLE_IO, TRACE1, \
     URL_PARAMS_PRIORITY, ZATO_NONE
from zato.common.audit_log import DataReceived, DataSent
from zato.common.const import ServiceConst
from zato.common.exception import HTTP_RESPONSES
from zato.common.hl7 import HL7Exception
from zato.common.json_internal import dumps, loads
from zato.common.json_schema import DictError as JSONSchemaDictError, ValidationException as JSONSchemaValidationException
from zato.common.marshal_.api import ModelValidationError
from zato.common.rate_limiting.common import AddressNotAllowed, BaseException as RateLimitingException, RateLimitReached
from zato.common.typing_ import cast_
from zato.common.util.exception import pretty_format_exception
from zato.server.connection.http_soap import BadRequest, ClientHTTPError, Forbidden, MethodNotAllowed, NotFound, \
     TooManyRequests, Unauthorized
from zato.server.service.internal import AdminService

# ################################################################################################################################

if 0:
    from zato.broker.client import BrokerClient
    from zato.common.typing_ import any_, anydict, anytuple, callable_, dictnone, stranydict, strlist, strstrdict
    from zato.server.service import Service
    from zato.server.base.parallel import ParallelServer
    from zato.server.base.worker import WorkerStore
    from zato.server.connection.http_soap.url_data import URLData
    BrokerClient = BrokerClient
    ParallelServer = ParallelServer
    Service = Service
    URLData = URLData

# ################################################################################################################################

logger = logging.getLogger(__name__)
_has_debug = logger.isEnabledFor(logging.DEBUG)
split_re = regex_compile('........?').findall # type: ignore

# ################################################################################################################################

accept_any_http = HTTP_SOAP.ACCEPT.ANY
accept_any_internal = HTTP_SOAP.ACCEPT.ANY_INTERNAL

# ################################################################################################################################

# https://tools.ietf.org/html/rfc6585
TOO_MANY_REQUESTS = 429

_status_bad_request = '{} {}'.format(BAD_REQUEST, HTTP_RESPONSES[BAD_REQUEST])
_status_internal_server_error = '{} {}'.format(INTERNAL_SERVER_ERROR, HTTP_RESPONSES[INTERNAL_SERVER_ERROR])
_status_not_found = '{} {}'.format(NOT_FOUND, HTTP_RESPONSES[NOT_FOUND])
_status_method_not_allowed = '{} {}'.format(METHOD_NOT_ALLOWED, HTTP_RESPONSES[METHOD_NOT_ALLOWED])
_status_unauthorized = '{} {}'.format(UNAUTHORIZED, HTTP_RESPONSES[UNAUTHORIZED])
_status_forbidden = '{} {}'.format(FORBIDDEN, HTTP_RESPONSES[FORBIDDEN])
_status_too_many_requests = '{} {}'.format(TOO_MANY_REQUESTS, HTTP_RESPONSES[TOO_MANY_REQUESTS])

# ################################################################################################################################

stack_format = None
_utcnow=datetime.utcnow

# ################################################################################################################################

_data_format_hl7 = HL7.Const.Version.v2.id

# ################################################################################################################################

_basic_auth = SEC_DEF_TYPE.BASIC_AUTH
_jwt = SEC_DEF_TYPE.JWT
_sso_ext_auth = _basic_auth, _jwt

# ################################################################################################################################

status_response = {}
for code, response in HTTP_RESPONSES.items():
    status_response[code] = '{} {}'.format(code, response)

# ################################################################################################################################

class ModuleCtx:
    Channel = CHANNEL.HTTP_SOAP
    No_URL_Match = (None, False)
    Rate_Limit_HTTP = RATE_LIMIT.OBJECT_TYPE.HTTP_SOAP
    Rate_Limit_SSO_User = RATE_LIMIT.OBJECT_TYPE.SSO_USER
    Exception_Separator = '*' * 80
    SIO_JSON = SIMPLE_IO.FORMAT.JSON
    Dict_Like = (DATA_FORMAT.JSON, DATA_FORMAT.DICT)

# ################################################################################################################################

response_404     = 'URL not found (CID:{})'
response_404_log = 'URL not found `%s` (Method:%s; Accept:%s; CID:%s)'

# ################################################################################################################################

def client_json_error(cid:'str', details:'any_') -> 'str':

    # This may be a tuple of arguments to an exception object
    if isinstance(details, tuple):
        exc_details = []
        for item in details: # type: ignore
            if isinstance(item, bytes):
                item = item.decode('utf8')
            exc_details.append(item)
    else:
        exc_details = details
        if isinstance(exc_details, bytes):
            exc_details = exc_details.decode('utf8')

    message = {'result':'Error', 'cid':cid} # type: stranydict
    if details:
        message['details'] = exc_details

    return dumps(message)

# ################################################################################################################################

client_error_wrapper = {
    DATA_FORMAT.JSON: client_json_error,
    HL7.Const.Version.v2.id: client_json_error,
}

# ################################################################################################################################

def get_client_error_wrapper(transport:'str', data_format:'str') -> 'callable_':
    try:
        result = client_error_wrapper[transport]
        return result
    except KeyError:
        # Any KeyError must be caught by the caller
        return client_error_wrapper[data_format]

# ################################################################################################################################

class _CachedResponse:
    """ A wrapper for responses served from caches.
    """
    __slots__ = ('payload', 'content_type', 'headers', 'status_code')

    def __init__(self, payload:'any_', content_type:'str', headers:'stranydict', status_code:'int') -> 'None':
        self.payload = payload
        self.content_type = content_type
        self.headers = headers
        self.status_code = status_code

# ################################################################################################################################

class _HashCtx:
    """ Encapsulates information needed to compute a hash value of an incoming request.
    """
    def __init__(
        self,
        raw_request:'str',
        channel_item:'any_',
        channel_params:'stranydict',
        wsgi_environ:'stranydict'
    ) -> 'None':
        self.raw_request = raw_request
        self.channel_item = channel_item
        self.channel_params = channel_params
        self.wsgi_environ = wsgi_environ

# ################################################################################################################################

class RequestDispatcher:
    """ Dispatches all the incoming HTTP requests to appropriate handlers.
    """
    def __init__(
        self,
        *,
        server:'ParallelServer',
        url_data:'URLData',
        request_handler:'RequestHandler',
        simple_io_config:'stranydict',
        return_tracebacks:'bool',
        default_error_message:'str',
        http_methods_allowed:'strlist'
    ) -> 'None':

        self.server = server
        self.url_data = url_data

        self.request_handler = request_handler
        self.simple_io_config = simple_io_config
        self.return_tracebacks = return_tracebacks

        self.default_error_message = default_error_message
        self.http_methods_allowed = http_methods_allowed

        # To reduce the number of attribute lookups
        self._sso_api_user = getattr(self.server, 'sso_api', None)

# ################################################################################################################################

    def dispatch(
        self,
        cid:'str',
        req_timestamp:'str',
        wsgi_environ:'stranydict',
        worker_store:'WorkerStore'
    ) -> 'any_':

        # Needed as one of the first steps
        http_method = wsgi_environ['REQUEST_METHOD']
        http_method = http_method if isinstance(http_method, str) else http_method.decode('utf8')

        http_accept = wsgi_environ.get('HTTP_ACCEPT') or accept_any_http
        http_accept = http_accept.replace('*', accept_any_internal).replace('/', 'HTTP_SEP')

        # Needed in later steps
        path_info = wsgi_environ['PATH_INFO']

        # Immediately reject the request if it is not a support HTTP method, no matter what channel
        # it would have otherwise matched.
        if http_method not in self.http_methods_allowed:
            wsgi_environ['zato.http.response.status'] = _status_method_not_allowed
            return client_json_error(cid, 'Unsupported HTTP method')

        # Can we recognize this URL path?
        # This gives us the URL info and security data - but note that here
        # we still haven't validated credentials, only matched the URL.
        # Credentials are checked in a call to self.url_data.check_security
        url_match, channel_item = self.url_data.match(path_info, http_method, http_accept) # type: ignore

        url_match = cast_('str', url_match)
        channel_item = cast_('anydict', channel_item)

        if _has_debug and channel_item:
            logger.debug('url_match:`%r`, channel_item:`%r`', url_match, sorted(channel_item.items()))

        # This is needed in parallel.py's on_wsgi_request
        wsgi_environ['zato.channel_item'] = channel_item

        # Read the raw data
        payload = wsgi_environ['wsgi.input'].read()

        # Store for later use prior to any kind of parsing
        wsgi_environ['zato.http.raw_request'] = payload

        # Assume that by default we are not authenticated / authorized
        auth_result = None

        # OK, we can possibly handle it
        if url_match not in ModuleCtx.No_URL_Match:

            try:

                # Raise 404 if the channel is inactive
                if not channel_item['is_active']:
                    logger.warning('url_data:`%s` is not active, raising NotFound', sorted(url_match.items()))
                    raise NotFound(cid, 'Channel inactive')

                # We need to read security info here so we know if POST needs to be
                # parsed. If so, we do it here and reuse it in other places
                # so it doesn't have to be parsed two or more times.
                post_data = {}

                match_target = channel_item['match_target']
                sec = self.url_data.url_sec[match_target]

                if sec.sec_def != ZATO_NONE or sec.sec_use_rbac is True:

                    if sec.sec_def != ZATO_NONE:

                        if sec.sec_def.sec_type == SEC_DEF_TYPE.OAUTH:
                            post_data.update(QueryDict(payload, encoding='utf-8'))

                    # Will raise an exception on any security violation
                    auth_result = self.url_data.check_security(
                        sec, cid, channel_item, path_info, payload, wsgi_environ, post_data, worker_store)

                # Check rate limiting now - this could not have been done earlier because we wanted
                # for security checks to be made first. Otherwise, someone would be able to invoke
                # our endpoint without credentials as many times as it is needed to exhaust the rate limit
                # denying in this manner access to genuine users.
                if channel_item.get('is_rate_limit_active'):
                    self.server.rate_limiting.check_limit(
                        cid, ModuleCtx.Rate_Limit_HTTP, channel_item['name'], wsgi_environ['zato.http.remote_addr'])

                # Store data received in audit log now - again, just like we rate limiting, we did not want to do it too soon.
                if channel_item.get('is_audit_log_received_active'):

                    # Describe our event ..
                    data_event = DataReceived()
                    data_event.type_ = ModuleCtx.Channel
                    data_event.object_id = channel_item['id']
                    data_event.data = payload
                    data_event.timestamp = req_timestamp
                    data_event.msg_id = cid

                    # .. and store it in the audit log.
                    self.server.audit_log.store_data_received(data_event)

                # Security definition-based checks went fine but it is still possible
                # that this sec_def is linked to an SSO user whose rate limits we need to check.

                # Check SSO-related limits only if SSO is enabled
                if self._sso_api_user:

                    # Not all sec_def types may have associated SSO users
                    if sec.sec_def != ZATO_NONE:

                        if sec.sec_def.sec_type in _sso_ext_auth:

                            # JWT comes with external sessions whereas Basic Auth does not
                            if auth_result and sec.sec_def.sec_type and hasattr(auth_result, 'raw_token'):
                                ext_session_id = auth_result.raw_token
                            else:
                                ext_session_id = None

                            # Try to log in the user to SSO by that account's external credentials.
                            self.server.sso_tool.on_external_auth(
                                sec.sec_def.sec_type, sec.sec_def.id, sec.sec_def.username, cid,
                                wsgi_environ, ext_session_id)
                        else:
                            raise Exception('Unexpected sec_type `{}`'.format(sec.sec_def.sec_type))

                # This is handy if someone invoked URLData's OAuth API manually
                wsgi_environ['zato.oauth.post_data'] = post_data

                # OK, no security exception at that point means we can finally invoke the service.
                response = self.request_handler.handle(cid, url_match, channel_item, wsgi_environ,
                    payload, worker_store, self.simple_io_config, post_data, path_info)

                wsgi_environ['zato.http.response.headers']['Content-Type'] = response.content_type
                wsgi_environ['zato.http.response.headers'].update(response.headers)
                wsgi_environ['zato.http.response.status'] = status_response[response.status_code]

                if channel_item['content_encoding'] == 'gzip':

                    s = StringIO()
                    with GzipFile(fileobj=s, mode='w') as f: # type: ignore
                        f.write(response.payload)
                    response.payload = s.getvalue()
                    s.close()

                    wsgi_environ['zato.http.response.headers']['Content-Encoding'] = 'gzip'

                # Store data sent in audit
                if channel_item.get('is_audit_log_sent_active'):

                    # Describe our event ..
                    data_event = DataSent()
                    data_event.type_ = ModuleCtx.Channel
                    data_event.object_id = channel_item['id']
                    data_event.data = response.payload
                    data_event.timestamp = _utcnow()
                    data_event.msg_id = 'zrp{}'.format(cid) # This is a response to this CID
                    data_event.in_reply_to = cid

                    # .. and store it in the audit log.
                    self.server.audit_log.store_data_sent(data_event)

                # Finally, return payload to the client
                return response.payload

            except Exception as e:
                _format_exc = format_exc()
                status = _status_internal_server_error

                if isinstance(e, (ClientHTTPError, ModelValidationError)):

                    response = e.msg
                    status_code = e.status

                    # TODO: Refactor this series of if/else's into a lookup dict.

                    if isinstance(e, Unauthorized):
                        status = _status_unauthorized
                        wsgi_environ['zato.http.response.headers']['WWW-Authenticate'] = e.challenge

                    elif isinstance(e, (BadRequest, ModelValidationError)):
                        status = _status_bad_request

                        # This is the channel that Dashboard uses and we want to return
                        # all the details in such cases because it is useful during development
                        if channel_item['name'] == MISC.DefaultAdminInvokeChannel:
                            response = e.msg
                        else:
                            response = e.msg if e.needs_msg else 'Invalid input'

                    elif isinstance(e, NotFound):
                        status = _status_not_found

                    elif isinstance(e, MethodNotAllowed):
                        status = _status_method_not_allowed

                    elif isinstance(e, Forbidden):
                        status = _status_forbidden

                    elif isinstance(e, TooManyRequests):
                        status = _status_too_many_requests

                else:

                    # JSON Schema validation
                    if isinstance(e, JSONSchemaValidationException):
                        status_code = _status_bad_request
                        needs_prefix = False if e.needs_err_details else True
                        response = JSONSchemaDictError(
                            cid, e.needs_err_details, e.error_msg, needs_prefix=needs_prefix).serialize()

                    # Rate limiting and whitelisting
                    elif isinstance(e, RateLimitingException):
                        response, status_code, status = self._on_rate_limiting_exception(e)

                    # HL7
                    elif channel_item['data_format'] == _data_format_hl7:
                        response, status_code, status = self._on_hl7_exception(e, channel_item)

                    else:
                        status_code = INTERNAL_SERVER_ERROR

                        # Same comment as in BadRequest, ModelValidationError above
                        if channel_item['name'] == MISC.DefaultAdminInvokeChannel:
                            wsgi_environ['zato.http.response.headers']['X-Zato-Message'] = str(e.args)
                            response = pretty_format_exception(e, cid)
                        else:
                            response = e.args if self.return_tracebacks else self.default_error_message

                _exc = stack_format(e, style='color', show_vals='like_source', truncate_vals=5000,
                    add_summary=True, source_lines=20) if stack_format else _format_exc # type: str

                # TODO: This should be configurable. Some people may want such
                # things to be on DEBUG whereas for others ERROR will make most sense
                # in given circumstances.
                logger.error(
                    'Caught an exception, cid:`%s`, status_code:`%s`, `%s`', cid, status_code, _exc)

                try:
                    error_wrapper = get_client_error_wrapper(channel_item['transport'], channel_item['data_format'])
                except KeyError:
                    # It is not a data format that we have a wrapper for.
                    if logger.isEnabledFor(TRACE1):
                        msg = 'No client error wrapper for transport:`{}`, data_format:`{}`'.format(
                            channel_item.get('transport'), channel_item.get('data_format'))
                        logger.log(TRACE1, msg)
                else:
                    response = error_wrapper(cid, response)

                wsgi_environ['zato.http.response.status'] = status

                return response

        # This is 404, no such URL path.
        else:

            # Indicate HTTP 404
            wsgi_environ['zato.http.response.status'] = _status_not_found

            # This is returned to the caller - note that it does not echo back the URL requested ..
            response = response_404.format(cid)

            # .. this goes to logs and it includes the URL sent by the client.
            logger.error(response_404_log, path_info, wsgi_environ.get('REQUEST_METHOD'), wsgi_environ.get('HTTP_ACCEPT'), cid)

            # This is the payload for the caller
            return response

# ################################################################################################################################

    def _on_rate_limiting_exception(self, e:'Exception') -> 'anytuple':

        if isinstance(e, RateLimitReached):
            status_code = TOO_MANY_REQUESTS
            status = _status_too_many_requests

        elif isinstance(e, AddressNotAllowed):
            status_code = FORBIDDEN
            status = _status_forbidden

        else:
            status_code = BAD_REQUEST
            status = _status_bad_request

        return 'Error {}'.format(status), status_code, status

# ################################################################################################################################

    def _on_hl7_exception(self, e:'Exception', channel_item:'stranydict') -> 'anytuple':

        if channel_item['should_return_errors'] and isinstance(e, HL7Exception):
            details = '`{}`; data:`{}`'.format(e.args[0], e.data)
        else:
            details = ''

        return details, BAD_REQUEST, _status_bad_request

# ################################################################################################################################

class RequestHandler:
    """ Handles individual HTTP requests to a given service.
    """
    def __init__(self, server:'ParallelServer') -> 'None':
        self.server = server

# ################################################################################################################################

    def _set_response_data(self, service:'Service', **kwargs:'any_'):
        """ A callback invoked by the services after it is done producing the response.
        """
        data_format = kwargs.get('data_format', '')
        transport = kwargs.get('transport', '')

        self.set_payload(service.response, data_format, transport, service)
        self.set_content_type(service.response, data_format)

        return service.response

# ################################################################################################################################

    def _get_flattened(self, params:'str') -> 'anydict':
        """ Returns a QueryDict of parameters with single-element lists unwrapped to point to the sole element directly.
        """
        out = {} # type: anydict

        if params:
            query_params = QueryDict(params, encoding='utf-8')

            for key, value in query_params.lists():
                if len(value) > 1:
                    out[key] = value
                else:
                    out[key] = value[0]

        return out

# ################################################################################################################################

    def create_channel_params(
        self,
        path_params:'strstrdict',
        channel_item:'any_',
        wsgi_environ:'stranydict',
        raw_request:'str',
        post_data:'dictnone'=None,
    ) -> 'strstrdict':
        """ Collects parameters specific to this channel (HTTP) and updates wsgi_environ
        with HTTP-specific data.
        """
        _qs = self._get_flattened(wsgi_environ.get('QUERY_STRING', ''))

        # Our caller has already parsed POST for us so we just use it as is
        if post_data:
            post = post_data
        else:
            # We cannot parse incoming data if we know for sure that an explicit
            # data format was set for channel.
            post = self._get_flattened(raw_request) if not channel_item.data_format else {}

        if channel_item.url_params_pri == URL_PARAMS_PRIORITY.QS_OVER_PATH:
            if _qs:
                path_params.update((key, value) for key, value in _qs.items())
            channel_params = path_params
        else:
            if _qs:
                channel_params = {key:value for key, value in _qs.items()}
            else:
                channel_params = {}
            channel_params.update(path_params)

        if _has_debug:
            logger.debug('channel_params `%s`, path_params `%s`, _qs `%s`', channel_params, path_params, _qs)

        wsgi_environ['zato.http.GET'] = _qs
        wsgi_environ['zato.http.POST'] = post

        return channel_params

# ################################################################################################################################

    def get_response_from_cache(
        self,
        service:'Service',
        raw_request:'str',
        channel_item:'any_',
        channel_params:'stranydict',
        wsgi_environ:'stranydict'
    ) -> 'anytuple':
        """ Returns a cached response for incoming request or None if there is nothing cached for it.
        By default, an incoming request's hash is calculated by sha256 over a concatenation of:
          * WSGI REQUEST_METHOD   # E.g. GET or POST
          * WSGI PATH_INFO        # E.g. /my/api
          * sorted(zato.http.GET) # E.g. ?foo=123&bar=456 (query string aka channel_params)
          * payload bytes         # E.g. '{"customer_id":"123"}' - a string object, before parsing
        Note that query string is sorted which means that ?foo=123&bar=456 is equal to ?bar=456&foo=123,
        that is, the order of parameters in query string does not matter.
        """
        if service.get_request_hash:
            hash_value = service.get_request_hash(_HashCtx(raw_request, channel_item, channel_params, wsgi_environ))
        else:
            query_string = str(sorted(channel_params.items()))
            data = '%s%s%s%s' % (wsgi_environ['REQUEST_METHOD'], wsgi_environ['PATH_INFO'], query_string, raw_request)
            hash_value = sha256(data.encode('utf8')).hexdigest()
            hash_value = '-'.join(split_re(hash_value)) # type: ignore

        # No matter if hash value is default or from service, always prefix it with channel's type and ID
        cache_key = 'http-channel-%s-%s' % (channel_item['id'], hash_value)

        # We have the key so now we can check if there is any matching response already stored in cache
        response = self.server.get_from_cache(channel_item['cache_type'], channel_item['cache_name'], cache_key)

        # If there is any response, we can now load into a format that our callers expect
        if response:
            response = loads(response)
            response = _CachedResponse(response['payload'], response['content_type'], response['headers'],
                response['status_code'])

        return cache_key, response

# ################################################################################################################################

    def set_response_in_cache(self, channel_item:'any_', key:'str', response:'any_'):
        """ Caches responses from this channel's invocation for as long as the cache is configured to keep it.
        """
        self.server.set_in_cache(channel_item['cache_type'], channel_item['cache_name'], key, dumps({
            'payload': response.payload,
            'content_type': response.content_type,
            'headers': response.headers,
            'status_code': response.status_code,
        }))

# ################################################################################################################################

    def handle(
        self,
        cid:'str',
        url_match:'any_',
        channel_item:'any_',
        wsgi_environ:'stranydict',
        raw_request:'str',
        worker_store:'WorkerStore',
        simple_io_config:'stranydict',
        post_data:'dictnone',
        path_info:'str'
    ) -> 'any_':
        """ Create a new instance of a service and invoke it.
        """
        service, is_active = self.server.service_store.new_instance(channel_item.service_impl_name)
        if not is_active:
            logger.warning('Could not invoke an inactive service:`%s`, cid:`%s`', service.get_name(), cid)
            raise NotFound(cid, response_404.format(
                path_info, wsgi_environ.get('REQUEST_METHOD'), wsgi_environ.get('HTTP_ACCEPT'), cid))

        if channel_item.merge_url_params_req:
            channel_params = self.create_channel_params(url_match, channel_item, wsgi_environ, raw_request, post_data)
        else:
            channel_params = {}

        # This is needed for type checking to make sure the name is bound
        cache_key = ''

        # If caching is configured for this channel, we need to first check if there is no response already
        if channel_item['cache_type']:
            cache_key, response = self.get_response_from_cache(service, raw_request, channel_item, channel_params, wsgi_environ)
            if response:
                return response

        # Add any path params matched to WSGI environment so it can be easily accessible later on
        wsgi_environ['zato.http.path_params'] = url_match

        # No cache for this channel or no cached response, invoke the service then.
        response = service.update_handle(self._set_response_data, service, raw_request,
            CHANNEL.HTTP_SOAP, channel_item.data_format, channel_item.transport, self.server,
            cast_('BrokerClient', worker_store.broker_client),
            worker_store, cid, simple_io_config, wsgi_environ=wsgi_environ,
            url_match=url_match, channel_item=channel_item, channel_params=channel_params,
            merge_channel_params=channel_item.merge_url_params_req,
            params_priority=channel_item.params_pri)

        # Cache the response if needed (cache_key was already created on return from get_response_from_cache)
        if channel_item['cache_type']:
            self.set_response_in_cache(channel_item, cache_key, response)

        # Having used the cache or not, we can return the response now
        return response

# ################################################################################################################################

    def _needs_admin_response(
        self,
        service_instance:'Service',
        service_invoker_name:'str'=ServiceConst.ServiceInvokerName # type: str
        ) -> 'bool':
        return isinstance(service_instance, AdminService) and service_instance.name != service_invoker_name

# ################################################################################################################################

    def set_payload(
        self,
        response:'any_',
        data_format:'str',
        transport:'str',
        service_instance:'Service'
    ) -> 'None':
        """ Sets the actual payload to represent the service's response out of what the service produced.
        This includes converting dictionaries into JSON or adding Zato metadata.
        """

        if self._needs_admin_response(service_instance):
            if data_format == ModuleCtx.SIO_JSON:
                zato_env = {'zato_env':{'result':response.result, 'cid':service_instance.cid, 'details':response.result_details}}
                if response.payload and (not isinstance(response.payload, str)):
                    payload = response.payload.getvalue(False)
                    payload.update(zato_env)
                else:
                    payload = zato_env

                response.payload = dumps(payload)
        else:
            if not isinstance(response.payload, str):
                if isinstance(response.payload, dict) and data_format in ModuleCtx.Dict_Like:
                    response.payload = dumps(response.payload)
                else:
                    if response.payload:
                        value = response.payload.getvalue() # type: ignore
                    else:
                        value = ''
                    response.payload = value

# ################################################################################################################################

    def set_content_type(
        self,
        response:'any_',
        data_format:'str'
    ) -> 'None':
        """ Sets a response's content type if one hasn't been supplied by the user.
        """
        # A user provided his or her own content type ..
        if response.content_type_changed:
            content_type = response.content_type
        else:
            # .. or they did not so let's find out if we're using SimpleIO ..
            if data_format == SIMPLE_IO.FORMAT.JSON:
                content_type = self.server.json_content_type

            # .. alright, let's use the default value after all.
            else:
                content_type = response.content_type

        response.content_type = content_type

# ################################################################################################################################
