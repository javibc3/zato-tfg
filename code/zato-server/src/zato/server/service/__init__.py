# -*- coding: utf-8 -*-

"""
Copyright (C) 2023, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from abc import ABCMeta, abstractmethod
import logging
from contextlib import closing
from datetime import datetime, timedelta
from http.client import BAD_REQUEST, METHOD_NOT_ALLOWED
from inspect import isclass
from traceback import format_exc
from typing import Optional as optional

# Amazon Braket SDK
from braket.circuits.circuit import Circuit
from braket.devices import LocalSimulator
from braket.aws.aws_device import AwsDevice, AwsSession
from braket.tasks.gate_model_quantum_task_result import GateModelQuantumTaskResult
from braket.tasks.annealing_quantum_task_result import AnnealingQuantumTaskResult
from braket.tasks.photonic_model_quantum_task_result import PhotonicModelQuantumTaskResult

# Boto3
from boto3 import Session

# Bunch
from bunch import bunchify

# lxml
from lxml.etree import _Element as EtreeElement
from lxml.objectify import ObjectifiedElement

# gevent
from gevent import Timeout, spawn
from gevent.lock import RLock

# Python 2/3 compatibility
from zato.common.py23_ import maxint

#Qiskit
from qiskit import QuantumCircuit, transpile
from qiskit.result import Result
from qiskit.providers.exceptions import QiskitBackendNotFoundError
from qiskit_ibm_provider import IBMProvider
from qiskit_ibm_provider.job.exceptions import IBMJobTimeoutError, IBMJobFailureError

# Zato
from zato.bunch import Bunch
from zato.common.api import BROKER, CHANNEL, DATA_FORMAT, HL7, KVDB, NO_DEFAULT_VALUE, PARAMS_PRIORITY, PUBSUB, \
     WEB_SOCKET, zato_no_op_marker
from zato.common.broker_message import CHANNEL as BROKER_MSG_CHANNEL
from zato.common.exception import Inactive, Reportable, ZatoException
from zato.common.json_internal import dumps
from zato.common.json_schema import ValidationException as JSONSchemaValidationException
from zato.common.typing_ import any_, cast_
from zato.common.util.api import make_repr, new_cid, payload_from_request, service_name_from_impl, spawn_greenlet, uncamelify
from zato.server.commands import CommandsFacade
from zato.server.connection.cache import CacheAPI
from zato.server.connection.email import EMailAPI
from zato.server.connection.facade import KeysightContainer, RESTFacade, SchedulerFacade
from zato.server.connection.jms_wmq.outgoing import WMQFacade
from zato.server.connection.search import SearchAPI
from zato.server.connection.sms import SMSAPI
from zato.server.connection.zmq_.outgoing import ZMQFacade
from zato.server.pattern.api import FanOut
from zato.server.pattern.api import InvokeRetry
from zato.server.pattern.api import ParallelExec
from zato.server.pubsub import PubSub
from zato.server.service.reqresp import AMQPRequestData, Cloud, Definition, HL7API, HL7RequestData, IBMMQRequestData, \
     InstantMessaging, Outgoing, Request
from zato.common.odb.model import SecurityBase

# Zato - Cython
from zato.cy.reqresp.payload import SimpleIOPayload
from zato.cy.reqresp.response import Response

# Not used here in this module but it's convenient for callers to be able to import everything from a single namespace
from zato.common.ext.dataclasses import dataclass
from zato.common.marshal_.api import Model, ModelCtx
from zato.simpleio import AsIs, CSV, Bool, Date, DateTime, Dict, Decimal, DictList, Elem as SIOElem, Float, Int, List, \
     Opaque, Text, UTC, UUID

# For pyflakes
AsIs = AsIs
CSV = CSV # type: ignore
Bool = Bool
dataclass = dataclass
Date = Date
DateTime = DateTime
Decimal = Decimal
Bool = Bool
Dict = Dict
DictList = DictList
Float = Float
Int = Int
List = List
Model = Model
ModelCtx = ModelCtx
Opaque = Opaque
Text = Text
UTC = UTC # type: ignore
UUID = UUID # type: ignore

# ################################################################################################################################

if 0:
    from logging import Logger
    from typing import Callable
    from zato.broker.client import BrokerClient
    from zato.common.audit import AuditPII
    from zato.common.crypto.api import ServerCryptoManager
    from zato.common.json_schema import Validator as JSONSchemaValidator
    from zato.common.kvdb.api import KVDB as KVDBAPI
    from zato.common.odb.api import ODBManager
    from zato.common.typing_ import any_, anydict, anydictnone, boolnone, callable_, dictnone, intnone, \
        stranydict, strnone, strlist
    from zato.common.util.time_ import TimeUtil
    from zato.distlock import Lock
    from zato.server.connection.ftp import FTPStore
    from zato.server.connection.web_socket import WebSocket
    from zato.server.base.worker import WorkerStore
    from zato.server.base.parallel import ParallelServer
    from zato.server.config import ConfigDict, ConfigStore
    from zato.server.connection.cassandra import CassandraAPI
    from zato.server.query import CassandraQueryAPI
    from zato.sso.api import SSOAPI
    from zato.simpleio import CySimpleIO

    AuditPII = AuditPII
    BrokerClient = BrokerClient
    Callable = Callable
    CassandraAPI = CassandraAPI
    CassandraQueryAPI = CassandraQueryAPI
    ConfigDict = ConfigDict
    ConfigStore = ConfigStore
    CySimpleIO = CySimpleIO
    FTPStore = FTPStore
    JSONSchemaValidator = JSONSchemaValidator
    KVDBAPI = KVDBAPI # type: ignore
    ODBManager = ODBManager
    ParallelServer = ParallelServer
    ServerCryptoManager = ServerCryptoManager
    SSOAPI = SSOAPI # type: ignore
    timedelta = timedelta
    TimeUtil = TimeUtil
    WebSocket = WebSocket
    WorkerStore = WorkerStore

# ################################################################################################################################

logger = logging.getLogger(__name__)
_get_logger=logging.getLogger

# ################################################################################################################################

NOT_GIVEN = 'ZATO_NOT_GIVEN'

# ################################################################################################################################

# Backward compatibility
Boolean = Bool
Integer = Int
ForceType = SIOElem
ListOfDicts = DictList
Nested = Opaque
Unicode = Text

# ################################################################################################################################

# For code completion
PubSub = PubSub

# ################################################################################################################################

_async_callback = CHANNEL.INVOKE_ASYNC_CALLBACK

# ################################################################################################################################

_wsgi_channels = {CHANNEL.HTTP_SOAP, CHANNEL.INVOKE, CHANNEL.INVOKE_ASYNC}

# ################################################################################################################################

_response_raw_types=(bytes, str, dict, list, tuple, EtreeElement, Model, ObjectifiedElement)
_utcnow = datetime.utcnow

# ################################################################################################################################

before_job_hooks = ('before_job', 'before_one_time_job', 'before_interval_based_job', 'before_cron_style_job')
after_job_hooks = ('after_job', 'after_one_time_job', 'after_interval_based_job', 'after_cron_style_job')
before_handle_hooks = ('before_handle',)
after_handle_hooks = ('after_handle', 'finalize_handle')

# The almost identical methods below are defined separately because they are used in critical paths
# where every if counts.

def call_hook_no_service(hook:'callable_') -> 'None':
    try:
        hook()
    except Exception:
        logger.error('Can\'t run hook `%s`, e:`%s`', hook, format_exc())

def call_hook_with_service(hook:'callable_', service:'Service') -> 'None':
    try:
        hook(service)
    except Exception:
        logger.error('Can\'t run hook `%s`, e:`%s`', hook, format_exc())

internal_invoke_keys = {'target', 'set_response_func', 'cid'}

# ################################################################################################################################

class ModuleCtx:
    HTTP_Channels = {CHANNEL.HTTP_SOAP, CHANNEL.INVOKE}
    Channel_Scheduler = CHANNEL.SCHEDULER
    Channel_Service = CHANNEL.SERVICE
    Pattern_Call_Channels = {CHANNEL.FANOUT_CALL, CHANNEL.PARALLEL_EXEC_CALL}

# ################################################################################################################################

@dataclass(init=False)
class AsyncCtx:
    """ Used by self.invoke_async to relay context of the invocation.
    """
    calling_service: str
    service_name: str
    cid: str
    data: str
    data_format: str
    callback: optional[list] = None
    zato_ctx: object
    environ: dict

# ################################################################################################################################

class ChannelInfo:
    """ Conveys information abouts the channel that a service is invoked through.
    Available in services as self.channel or self.chan.
    """
    __slots__ = ('id', 'name', 'type', 'data_format', 'is_internal', 'match_target', 'impl', 'security', 'sec')

    def __init__(
        self,
        id: 'intnone',
        name: 'strnone',
        type: 'strnone',
        data_format: 'strnone',
        is_internal: 'boolnone',
        match_target: 'any_',
        security: 'ChannelSecurityInfo',
        impl: 'any_'
    ) -> 'None':
        self.id = id
        self.name = name
        self.type = type
        self.data_format = data_format
        self.is_internal = is_internal
        self.match_target = match_target
        self.impl = impl
        self.security = self.sec = security

    def __repr__(self) -> 'str':
        return make_repr(self)

# ################################################################################################################################

class ChannelSecurityInfo:
    """ Contains information about a security definition assigned to a channel, if any.
    Available in services as:

    * self.channel.security
    * self.channel.sec

    * self.chan.security
    * self.chan.sec
    """
    __slots__ = ('id', 'name', 'type', 'username', 'impl')

    def __init__(self, id:'intnone', name:'strnone', type:'strnone', username:'strnone', impl:'any_') -> 'None':
        self.id = id
        self.name = name
        self.type = type
        self.username = username
        self.impl = impl

# ################################################################################################################################

    def to_dict(self, needs_impl:'bool'=False) -> 'stranydict':
        out = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'username': self.username,
        }

        if needs_impl:
            out['impl'] = self.impl

        return out

# ################################################################################################################################

class _WSXChannel:
    """ Provides communication with WebSocket channels.
    """
    def __init__(self, server:'ParallelServer', channel_name:'str') -> 'None':
        self.server = server
        self.channel_name = channel_name

# ################################################################################################################################

    def broadcast(self, data:'str', _action:'str'=BROKER_MSG_CHANNEL.WEB_SOCKET_BROADCAST.value) -> 'None':
        """ Sends data to all WSX clients connected to this channel.
        """
        # type: (str, str)

        # If we are invoked, it means that self.channel_name points to an existing object
        # so we can just let all servers know that they are to invoke their connected clients.
        self.server.broker_client.publish({
            'action': _action,
            'channel_name': self.channel_name,
            'data': data
        })

# ################################################################################################################################

class _WSXChannelContainer:
    """ A thin wrapper to mediate access to WebSocket channels.
    """
    def __init__(self, server:'ParallelServer') -> 'None':
        self.server = server
        self._lock = RLock()
        self._channels = {}

# ################################################################################################################################

    def __getitem__(self, channel_name):
        # type: (str) -> _WSXChannel
        with self._lock:
            if channel_name not in self._channels:
                if self.server.worker_store.web_socket_api.connectors.get(channel_name):
                    self._channels[channel_name] = _WSXChannel(self.server, channel_name)
                else:
                    raise KeyError('No such WebSocket channel `{}`'.format(channel_name))

            return self._channels[channel_name]

# ################################################################################################################################

    def get(self, channel_name:'str') -> '_WSXChannel | None':
        try:
            return self[channel_name]
        except KeyError:
            return None # Be explicit in returning None

# ################################################################################################################################

class WSXFacade:
    """ An object via which WebSocket channels and outgoing connections may be invoked or send broadcasts to.
    """
    __slots__ = 'server', 'channel', 'out'

    def __init__(self, server:'ParallelServer') -> 'None':
        self.server = server
        self.channel = _WSXChannelContainer(self.server)

# ################################################################################################################################

class AMQPFacade:
    """ Introduced solely to let service access outgoing connections through self.amqp.invoke/_async
    rather than self.out.amqp_invoke/_async. The .send method is kept for pre-3.0 backward-compatibility.
    """
    __slots__ = ('send', 'invoke', 'invoke_async')

# ################################################################################################################################

class PatternsFacade:
    """ The API through which services make use of integration patterns.
    """
    __slots__ = ('invoke_retry', 'fanout', 'parallel')

    def __init__(self, invoking_service:'Service', cache:'anydict', lock:'RLock') -> 'None':
        self.invoke_retry = InvokeRetry(invoking_service)
        self.fanout = FanOut(invoking_service, cache, lock)
        self.parallel = ParallelExec(invoking_service, cache, lock)

# ################################################################################################################################

class Service:
    """ A base class for all services deployed on Zato servers, no matter the transport and protocol, be it REST, IBM MQ
    or any other, regardless whether they arere built-in or user-defined ones.
    """
    rest: 'RESTFacade'
    schedule: 'SchedulerFacade'

    call_hooks:'bool' = True
    _filter_by = None
    enforce_service_invokes: 'bool'
    invokes = []
    http_method_handlers = {}

    # Class-wide attributes shared by all services thus created here instead of assigning to self.
    cloud = Cloud()
    definition = Definition()
    im = InstantMessaging()
    odb:'ODBManager'
    kvdb:'KVDB'
    pubsub:'PubSub'
    static_config:'Bunch'

    email:'EMailAPI | None' = None
    search:'SearchAPI | None' = None
    patterns: 'PatternsFacade | None' = None
    cassandra_conn:'CassandraAPI | None' = None
    cassandra_query:'CassandraQueryAPI | None' = None

    amqp = AMQPFacade()
    commands = CommandsFacade()

    # For WebSockets
    wsx:'WSXFacade'

    _worker_store:'WorkerStore'
    _worker_config:'ConfigStore'
    _out_ftp:'FTPStore'
    _out_plain_http:'ConfigDict'

    _has_before_job_hooks:'bool' = False
    _has_after_job_hooks:'bool' = False
    _before_job_hooks = []
    _after_job_hooks = []

    has_sio:'bool'

    # Cython based SimpleIO definition created by service store when the class is deployed
    _sio:'CySimpleIO'

    # Rate limiting
    _has_rate_limiting:'bool' = False

    # User management and SSO
    sso:'SSOAPI'

    # Crypto operations
    crypto:'ServerCryptoManager'

    # Audit log
    audit_pii:'AuditPII'

    # Vendors - Keysight
    keysight: 'KeysightContainer'

    # By default, services do not use JSON Schema
    schema = '' # type: str

    # JSON Schema validator attached only if service declares a schema to use
    _json_schema_validator:'JSONSchemaValidator | None' = None

    server: 'ParallelServer'
    broker_client: 'BrokerClient'
    time: 'TimeUtil'

    # These two are the same
    chan: 'ChannelInfo'
    channel: 'ChannelInfo'

    # When was the service invoked
    invocation_time: 'datetime'

    # When did our 'handle' method finished processing the request
    handle_return_time: 'datetime'

    # # A timedelta object with the processing time up to microseconds
    processing_time_raw: 'timedelta'

    # Processing time in milliseconds
    processing_time: 'float'

    component_enabled_sms: 'bool'
    component_enabled_hl7: 'bool'
    component_enabled_odoo: 'bool'
    component_enabled_email: 'bool'
    component_enabled_search: 'bool'
    component_enabled_ibm_mq: 'bool'
    component_enabled_zeromq: 'bool'
    component_enabled_msg_path: 'bool'
    component_enabled_patterns: 'bool'
    component_enabled_target_matcher: 'bool'
    component_enabled_invoke_matcher: 'bool'

    cache: 'CacheAPI'

    def __init__(
        self,
        *ignored_args:'any_',
        **ignored_kwargs:'any_'
    ) -> 'None':
        self.name = self.__class__.__service_name # Will be set through .get_name by Service Store
        self.impl_name = self.__class__.__service_impl_name # Ditto
        self.logger = _get_logger(self.name) # type: Logger
        self.cid = ''
        self.in_reply_to = ''
        self.data_format = ''
        self.transport = ''
        self.wsgi_environ = {} # type: anydict
        self.job_type = ''     # type: str
        self.environ = Bunch()
        self.request = Request(self) # type: Request
        self.response = Response(self.logger) # type: ignore
        self.user_config = Bunch()
        self.has_validate_input = False
        self.has_validate_output = False

        self.usage = 0 # How many times the service has been invoked
        self.slow_threshold = maxint # After how many ms to consider the response came too late

        self.out = self.outgoing = Outgoing(
            self.amqp,
            self._out_ftp,
            WMQFacade(self) if self.component_enabled_ibm_mq else None,
            self._worker_config.out_odoo,
            self._out_plain_http,
            self._worker_config.out_soap,
            self._worker_store.sql_pool_store,
            ZMQFacade(self._worker_store.zmq_out_api) if self.component_enabled_zeromq else NO_DEFAULT_VALUE,
            self._worker_store.outconn_wsx,
            self._worker_store.vault_conn_api,
            SMSAPI(self._worker_store.sms_twilio_api) if self.component_enabled_sms else None,
            self._worker_config.out_sap,
            self._worker_config.out_sftp,
            self._worker_store.outconn_ldap,
            self._worker_store.outconn_mongodb,
            self._worker_store.def_kafka,
            self.kvdb
        ) # type: Outgoing

        # REST facade for outgoing connections
        self.rest = RESTFacade()

        if self.component_enabled_hl7:
            hl7_api = HL7API(self._worker_store.outconn_hl7_fhir, self._worker_store.outconn_hl7_mllp)
            self.out.hl7 = hl7_api

# ################################################################################################################################

    @staticmethod
    def get_name_static(class_:'type[Service]') -> 'str':
        return Service.get_name(class_) # type: ignore

# ################################################################################################################################

    @classmethod
    def get_name(class_:'type[Service]') -> 'str':
        """ Returns a service's name, settings its .name attribute along. This will
        be called once while the service is being deployed.
        """
        if not hasattr(class_, '__service_name'):
            name = getattr(class_, 'name', None)
            if not name:
                name = service_name_from_impl(class_.get_impl_name())
                name = class_.convert_impl_name(name)

            class_.__service_name = name # type: str

        return class_.__service_name

# ################################################################################################################################

    @classmethod
    def get_impl_name(class_:'type[Service]') -> 'str':
        if not hasattr(class_, '__service_impl_name'):
            class_.__service_impl_name = '{}.{}'.format(class_.__module__, class_.__name__)
        return class_.__service_impl_name

# ################################################################################################################################

    @staticmethod
    def convert_impl_name(name:'str') -> 'str':
        # TODO: Move the replace functionality over to uncamelify, possibly modifying its regexp
        split = uncamelify(name).split('.')

        path, class_name = split[:-1], split[-1]
        path = [elem.replace('_', '-') for elem in path]

        class_name = class_name[1:] if class_name.startswith('-') else class_name
        class_name = class_name.replace('.-', '.').replace('_-', '_')

        return '{}.{}'.format('.'.join(path), class_name)

# ################################################################################################################################

    @classmethod
    def add_http_method_handlers(class_:'type[Service]') -> 'None':

        for name in dir(class_):
            if name.startswith('handle_'):

                if not getattr(class_, 'http_method_handlers', False):
                    class_.http_method_handlers = {}

                method = name.replace('handle_', '')
                class_.http_method_handlers[method] = getattr(class_, name)

# ################################################################################################################################

    def _init(self, may_have_wsgi_environ:'bool'=False) -> 'None':
        """ Actually initializes the service.
        """
        self.slow_threshold = self.server.service_store.services[self.impl_name]['slow_threshold']

        # The if's below are meant to be written in this way because we don't want any unnecessary attribute lookups
        # and method calls in this method - it's invoked each time a service is executed. The attributes are set
        # for the whole of the Service class each time it is discovered they are needed. It cannot be done in ServiceStore
        # because at the time that ServiceStore executes the worker config may still not be ready.

        if self.component_enabled_email:
            if not Service.email:
                Service.email = EMailAPI(self._worker_store.email_smtp_api, self._worker_store.email_imap_api)

        if self.component_enabled_search:
            if not Service.search:
                Service.search = SearchAPI(self._worker_store.search_es_api, self._worker_store.search_solr_api)

        if self.component_enabled_patterns:
            self.patterns = PatternsFacade(self, self.server.internal_cache_patterns, self.server.internal_cache_lock_patterns)

        if may_have_wsgi_environ:
            self.request.http.init(self.wsgi_environ)

        # self.has_sio attribute is set by ServiceStore during deployment
        if self.has_sio:
            self.request.init(True, self.cid, self._sio, self.data_format, self.transport, self.wsgi_environ, self.server.encrypt)
            self.response.init(self.cid, self._sio, self.data_format)

        # Cache is always enabled
        self.cache = self._worker_store.cache_api

        # REST facade
        self.rest.init(self.cid, self._out_plain_http)

        # Vendors - Keysight
        self.keysight = KeysightContainer()
        self.keysight.init(self.cid, self._out_plain_http)

# ################################################################################################################################

    def set_response_data(self, service:'Service', **kwargs:'any_') -> 'any_':
        response = service.response.payload
        if not isinstance(response, _response_raw_types):

            if hasattr(response, 'getvalue'):
                response = response.getvalue(serialize=kwargs.get('serialize'))
                if kwargs.get('as_bunch'):
                    response = bunchify(response)

            elif hasattr(response, 'to_dict'):
                response = response.to_dict()

            elif hasattr(response, 'to_json'):
                response = response.to_json()

            service.response.payload = response

        return response

# ################################################################################################################################

    def _invoke(self, service:'Service', channel:'str') -> 'None':
        #
        # If channel is HTTP and there are any per-HTTP verb methods, it means we want for the service to be a REST target.
        # Let's say it is POST. If we have handle_POST, it is invoked. If there is no handle_POST,
        # '405 Method Not Allowed is returned'.
        #
        # However, if we have 'handle' only, it means this is always invoked and no default 405 is returned.
        #
        # In short, implement handle_* if you want REST behaviour. Otherwise, keep everything in handle.
        #

        # Ok, this is HTTP
        if channel in ModuleCtx.HTTP_Channels:

            # We have at least one per-HTTP verb handler
            if service.http_method_handlers:

                # But do we have any handler matching current request's verb?
                if service.request.http.method in service.http_method_handlers:

                    # Yes, call the handler
                    service.http_method_handlers[service.request.http.method](service)

                # No, return 405
                else:
                    service.response.status_code = METHOD_NOT_ALLOWED

            # We have no custom handlers so we always call 'handle'
            else:
                service.handle()

        # It's not HTTP so we simply call 'handle'
        else:
            service.handle()

# ################################################################################################################################

    def extract_target(self, name:'str') -> 'tuple[str, str]':
        """ Splits a service's name into name and target, if the latter is provided on input at all.
        """
        # It can be either a name or a name followed by the target to invoke the service on,
        # i.e. 'myservice' or 'myservice@mytarget'.
        if '@' in name:
            name, target = name.split('@')
            if not target:
                raise ZatoException(self.cid, 'Target must not be empty in `{}`'.format(name))
        else:
            target = ''

        return name, target

# ################################################################################################################################

    def update_handle(self,
        set_response_func, # type: Callable
        service,       # type: Service
        raw_request,   # type: any_
        channel,       # type: str
        data_format,   # type: str
        transport,     # type: str
        server,        # type: ParallelServer
        broker_client, # type: BrokerClient | None
        worker_store,  # type: WorkerStore
        cid,           # type: str
        simple_io_config, # type: anydict
        *args:'any_',
        **kwargs:'any_'
    ) -> 'any_':

        wsgi_environ = kwargs.get('wsgi_environ', {})
        payload = wsgi_environ.get('zato.request.payload')
        channel_item = wsgi_environ.get('zato.channel_item', {})

        zato_response_headers_container = kwargs.get('zato_response_headers_container')

        # Here's an edge case. If a SOAP request has a single child in Body and this child is an empty element
        # (though possibly with attributes), checking for 'not payload' alone won't suffice - this evaluates
        # to False so we'd be parsing the payload again superfluously.
        if not isinstance(payload, ObjectifiedElement) and not payload:
            payload = payload_from_request(server.json_parser, cid, raw_request, data_format, transport, channel_item)

        job_type = kwargs.get('job_type') or ''
        channel_params = kwargs.get('channel_params', {})
        merge_channel_params = kwargs.get('merge_channel_params', True)
        params_priority = kwargs.get('params_priority', PARAMS_PRIORITY.DEFAULT)

        service.update(service, channel, server, broker_client,
            worker_store, cid, payload, raw_request, transport, simple_io_config, data_format, wsgi_environ,
            job_type=job_type, channel_params=channel_params,
            merge_channel_params=merge_channel_params, params_priority=params_priority,
            in_reply_to=wsgi_environ.get('zato.request_ctx.in_reply_to', None), environ=kwargs.get('environ'),
            wmq_ctx=kwargs.get('wmq_ctx'), channel_info=kwargs.get('channel_info'),
            channel_item=channel_item, wsx=wsgi_environ.get('zato.wsx'))

        # It's possible the call will be completely filtered out. The uncommonly looking not self.accept shortcuts
        # if ServiceStore replaces self.accept with None in the most common case of this method's not being
        # implemented by user services.
        if (not self.accept) or service.accept():

            # Assumes it goes fine by default
            e, exc_formatted = None, None

            try:

                # Check rate limiting first - note the usage of 'service' rather than 'self',
                # in case self is a gateway service such as an JSON-RPC one in which case
                # we are in fact interested in checking the target service's rate limit,
                # not our own.
                if service._has_rate_limiting:
                    self.server.rate_limiting.check_limit(self.cid, ModuleCtx.Channel_Service, service.name,
                        self.wsgi_environ['zato.http.remote_addr'])

                if service.server.component_enabled.stats:
                    service.server.current_usage.incr(service.name)

                service.invocation_time = _utcnow()

                # Check if there is a JSON Schema validator attached to the service and if so,
                # validate input before proceeding any further.
                if service._json_schema_validator and service._json_schema_validator.is_initialized:
                    validation_result = service._json_schema_validator.validate(cid, raw_request)
                    if not validation_result:
                        error = validation_result.get_error()

                        error_msg = error.get_error_message()
                        error_msg_details = error.get_error_message(True)

                        raise JSONSchemaValidationException(cid, CHANNEL.SERVICE, service.name,
                            error.needs_err_details, error_msg, error_msg_details)

                # All hooks are optional so we check if they have not been replaced with None by ServiceStore.

                # Call before job hooks if any are defined and we are called from the scheduler
                if service.call_hooks and service._has_before_job_hooks and self.channel.type == ModuleCtx.Channel_Scheduler:
                    for elem in service._before_job_hooks:
                        if elem:
                            call_hook_with_service(elem, service)

                # Called before .handle - catches exceptions
                if service.call_hooks and service.before_handle:
                    call_hook_no_service(service.before_handle)

                # Called before .handle - does not catch exceptions
                if service.validate_input:
                    service.validate_input()

                # This is the place where the service is invoked
                self._invoke(service, channel)

                # Called after .handle - does not catch exceptions
                if service.validate_output:
                    service.validate_output()

                # Called after .handle - catches exceptions
                if service.call_hooks and service.after_handle:
                    call_hook_no_service(service.after_handle)

                # Call after job hooks if any are defined and we are called from the scheduler
                if service._has_after_job_hooks and self.channel.type == ModuleCtx.Channel_Scheduler:
                    for elem in service._after_job_hooks:
                        if elem:
                            call_hook_with_service(elem, service)

                # Optional, almost never overridden.
                if service.finalize_handle:
                    call_hook_no_service(service.finalize_handle)

            except Exception as ex:
                e = ex
                exc_formatted = format_exc()
            finally:
                try:

                    # This obtains the response
                    response = set_response_func(service, data_format=data_format, transport=transport, **kwargs)

                    # If this was fan-out/fan-in we need to always notify our callbacks no matter the result
                    if channel in ModuleCtx.Pattern_Call_Channels:

                        if channel == CHANNEL.FANOUT_CALL:
                            fanout = self.patterns.fanout # type: ignore
                            func = fanout.on_call_finished
                            exc_data = e
                        else:
                            parallel = self.patterns.parallel # type: ignore
                            func = parallel.on_call_finished
                            exc_data = exc_formatted

                        if isinstance(service.response.payload, SimpleIOPayload):
                            payload = service.response.payload.getvalue()
                        else:
                            payload = service.response.payload

                        spawn_greenlet(func, service, payload, exc_data)

                    # It is possible that, on behalf of our caller (e.g. pub.zato.service.service-invoker),
                    # we also need to populate a dictionary of headers that were produced by the service
                    # that we are invoking.
                    if zato_response_headers_container is not None:
                        if service.response.headers:
                            zato_response_headers_container.update(service.response.headers)

                except Exception as resp_e:

                    if e:
                        if isinstance(e, Reportable):
                            raise e
                        else:
                            raise Exception(exc_formatted)
                    raise resp_e

                else:
                    if e:
                        raise e from None

        # We don't accept it but some response needs to be returned anyway.
        else:
            response = service.response
            response.payload = ''
            response.status_code = BAD_REQUEST

        # If we are told always to skip response elements, this is where we make use of it.
        _zato_needs_response_wrapper = getattr(service.__class__, '_zato_needs_response_wrapper', None)
        if _zato_needs_response_wrapper is False:
            kwargs['skip_response_elem'] = True

        if kwargs.get('skip_response_elem') and hasattr(response, 'keys'):

            # If if has .keys, it means it is a dict.
            response = cast_('dict', response)

            keys = list(response)
            try:
                keys.remove('_meta')
            except ValueError:
                # This is fine, there was only the actual response element here,
                # without the '_meta' pagination
                pass

            # It is possible that the dictionary is empty
            response_elem = keys[0] if keys else None

            # This covers responses that have only one top-level element
            # and that element's name is 'response' or, e.g. 'zato_amqp_...'
            if len(keys) == 1:
                if response_elem == 'response' or (isinstance(response_elem, str) and response_elem.startswith('zato')):
                    return response[response_elem]

                # This may be a dict response from a service, in which case we return it as is
                elif isinstance(response, dict):
                    return response

            # .. otherwise, this could be a dictionary of elements other than the above
            # so we just return the dict as it is.
            else:
                return response

        else:
            return response

# ################################################################################################################################

    def invoke_by_impl_name(
        self,
        impl_name,  # type: str
        payload='', # type: str | dict
        channel=CHANNEL.INVOKE,       # type: str
        data_format=DATA_FORMAT.DICT, # type: str
        transport='',       # type: str
        serialize=False,    # type: bool
        as_bunch=False,     # type: bool
        timeout=0,          # type: int
        raise_timeout=True, # type: bool
        **kwargs:'any_'
    ) -> 'any_':
        """ Invokes a service synchronously by its implementation name (full dotted Python name).
        """
        if self.component_enabled_target_matcher:

            orig_impl_name = impl_name
            impl_name, target = self.extract_target(impl_name)

            # It's possible we are being invoked through self.invoke or self.invoke_by_id
            target = target or kwargs.get('target', '')

            if not self._worker_store.target_matcher.is_allowed(target):
                raise ZatoException(self.cid, 'Invocation target `{}` not allowed ({})'.format(target, orig_impl_name))

        if self.component_enabled_invoke_matcher:
            if not self._worker_store.invoke_matcher.is_allowed(impl_name):
                raise ZatoException(self.cid, 'Service `{}` (impl_name) cannot be invoked'.format(impl_name))

        if self.impl_name == impl_name:
            msg = 'A service cannot invoke itself, name:[{}]'.format(self.name)
            self.logger.error(msg)
            raise ZatoException(self.cid, msg)

        service, is_active = self.server.service_store.new_instance(impl_name)
        if not is_active:
            raise Inactive(service.get_name())

        # If there is no payload but there are keyword arguments other than what we expect internally,
        # we can turn them into a payload ourselves.
        if not payload:
            kwargs_keys = set(kwargs)

            # Substracting keys that are known from the keys that are given on input
            # gives us a set of keys that we do not know, i.e. the keys that are extra
            # and that can be turned into a payload.
            extra_keys = kwargs_keys - internal_invoke_keys

            # Now, if the substraction did result in any keys, we can for sure build a dictionary with payload data.
            if extra_keys:
                payload = {}
                for name in extra_keys:
                    payload[name] = kwargs[name]

        set_response_func = kwargs.pop('set_response_func', service.set_response_data)

        invoke_args = (set_response_func, service, payload, channel, data_format, transport, self.server,
            self.broker_client, self._worker_store, kwargs.pop('cid', self.cid), {})

        kwargs.update({'serialize':serialize, 'as_bunch':as_bunch})

        if timeout:
            g = None
            try:
                g = spawn(self.update_handle, *invoke_args, **kwargs)
                return g.get(block=True, timeout=timeout)
            except Timeout:
                if g:
                    g.kill()
                logger.warning('Service `%s` timed out (%s)', service.name, self.cid)
                if raise_timeout:
                    raise
        else:
            return self.update_handle(*invoke_args, **kwargs)

# ################################################################################################################################

    def invoke(self, zato_name:'any_', *args:'any_', **kwargs:'any_') -> 'any_':
        """ Invokes a service synchronously by its name.
        """
        # The 'zato_name' parameter is actually a service class,
        # not its name, and we need to extract the name ourselves.
        if isclass(zato_name) and issubclass(zato_name, Service): # type: Service
            zato_name = zato_name.get_name()

        if self.component_enabled_target_matcher:
            zato_name, target = self.extract_target(zato_name) # type: ignore
            kwargs['target'] = target

        if self._enforce_service_invokes and self.invokes:
            if zato_name not in self.invokes:
                msg = 'Could not invoke `{}` which is not in `{}`'.format(zato_name, self.invokes)
                self.logger.warning(msg)
                raise ValueError(msg)

        return self.invoke_by_impl_name(self.server.service_store.name_to_impl_name[zato_name], *args, **kwargs)

# ################################################################################################################################

    def invoke_by_id(self, service_id:'int', *args:'any_', **kwargs:'any_') -> 'any_':
        """ Invokes a service synchronously by its ID.
        """
        if self.component_enabled_target_matcher:
            service_id, target = self.extract_target(service_id) # type: ignore
            kwargs['target'] = target

        return self.invoke_by_impl_name(self.server.service_store.id_to_impl_name[service_id], *args, **kwargs)

# ################################################################################################################################

    def invoke_async(
        self,
        name,       # type: str
        payload='', # type: str
        channel=CHANNEL.INVOKE_ASYNC, # type: str
        data_format=DATA_FORMAT.DICT, # type: str
        transport='', # type: str
        expiration=BROKER.DEFAULT_EXPIRATION, # type: int
        to_json_string=False, # type: bool
        cid='',        # type: str
        callback=None, # type: str | Service | None
        zato_ctx=None, # type: stranydict | None
        environ=None   # type: stranydict | None
    ) -> 'str':
        """ Invokes a service asynchronously by its name.
        """

        zato_ctx = zato_ctx if zato_ctx is not None else {}
        environ = environ if environ is not None else {}

        if self.component_enabled_target_matcher:
            name, target = self.extract_target(name)
            zato_ctx['zato.request_ctx.target'] = target
        else:
            target = None

        # Let's first find out if the service can be invoked at all
        impl_name = self.server.service_store.name_to_impl_name[name]

        if self.component_enabled_invoke_matcher:
            if not self._worker_store.invoke_matcher.is_allowed(impl_name):
                raise ZatoException(self.cid, 'Service `{}` (impl_name) cannot be invoked'.format(impl_name))

        if to_json_string:
            payload = dumps(payload)

        cid = cid or new_cid()

        # If there is any callback at all, we need to figure out its name because that's how it will be invoked by.
        if callback:

            # The same service
            if callback is self:
                callback = self.name

        else:
            sink = '{}-async-callback'.format(self.name)
            if sink in self.server.service_store.name_to_impl_name:
                callback = sink

            else:
                # Otherwise the callback must be a string pointing to the actual service to reply to
                # so we do not need to do anything.
                pass

        async_ctx = AsyncCtx()
        async_ctx.calling_service = self.name
        async_ctx.service_name = name
        async_ctx.cid = cid
        async_ctx.data = payload
        async_ctx.data_format = data_format
        async_ctx.zato_ctx = zato_ctx
        async_ctx.environ = environ

        if callback:
            async_ctx.callback = list(callback) if isinstance(callback, (list, tuple)) else [callback]

        spawn_greenlet(self._invoke_async, async_ctx, channel)

        return cid

# ################################################################################################################################

    def _invoke_async(
        self,
        ctx,     # type: AsyncCtx
        channel, # type: str
        _async_callback=_async_callback, # type: Service | str
    ) -> 'None':

        # Invoke our target service ..
        response = self.invoke(ctx.service_name, ctx.data, data_format=ctx.data_format, channel=channel, skip_response_elem=True)

        # .. and report back the response to our callback(s), if there are any.
        if ctx.callback:
            for callback_service in ctx.callback: # type: str
                _ = self.invoke(callback_service, payload=response, channel=_async_callback, cid=new_cid,
                    data_format=ctx.data_format, in_reply_to=ctx.cid, environ=ctx.environ,
                    skip_response_elem=True)

# ################################################################################################################################

    def translate(self, *args:'any_', **kwargs:'any_') -> 'str':
        raise NotImplementedError('An initializer should override this method')

# ################################################################################################################################

    def handle(self) -> 'None':
        """ The only method Zato services need to implement in order to process
        incoming requests.
        """
        raise NotImplementedError('Should be overridden by subclasses (Service.handle)')

# ################################################################################################################################

    def lock(self, name:'str'='', *args:'any_', **kwargs:'any_') -> 'Lock':
        """ Creates a distributed lock.

        name - defaults to self.name effectively making access to this service serialized
        ttl - defaults to 20 seconds and is the max time the lock will be held
        block - how long (in seconds) we will wait to acquire the lock before giving up
        """

        # The relevant part of signature in 2.0 was `expires=20, timeout=10`
        # and the 3.0 -> 2.0 mapping is: ttl->expires, block=timeout

        if not args:
            ttl = kwargs.get('ttl') or kwargs.get('expires') or 20
            block = kwargs.get('block') or kwargs.get('timeout') or 10
        else:
            if len(args) == 1:
                ttl = args[0]
                block = 10
            else:
                ttl = args[0]
                block = args[1]

        return self.server.zato_lock_manager(name or self.name, ttl=ttl, block=block)

# ################################################################################################################################

    def accept(self, _zato_no_op_marker:'any_'=zato_no_op_marker) -> 'bool':
        return True

# ################################################################################################################################

    def spawn(self, *args:'any_', **kwargs:'any_') -> 'any_':
        return spawn(*args, **kwargs)

# ################################################################################################################################

    @classmethod
    def before_add_to_store(cls, logger:'Logger') -> 'bool':
        """ Invoked right before the class is added to the service store.
        """
        return True

    def before_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked  if the service has been defined as a job's invocation target,
        regardless of the job's type.
        """

    def before_one_time_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked if the service has been defined as a one-time job's
        invocation target.
        """

    def before_interval_based_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked if the service has been defined as an interval-based job's
        invocation target.
        """

    def before_cron_style_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked if the service has been defined as a cron-style job's
        invocation target.
        """

    def before_handle(self, _zato_no_op_marker=zato_no_op_marker, *args, **kwargs): # type: ignore
        """ Invoked just before the actual service receives the request data.
        """

    def after_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked  if the service has been defined as a job's invocation target,
        regardless of the job's type.
        """

    def after_one_time_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked if the service has been defined as a one-time job's
        invocation target.
        """

    def after_interval_based_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked if the service has been defined as an interval-based job's
        invocation target.
        """

    def after_cron_style_job(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked if the service has been defined as a cron-style job's
        invocation target.
        """

    def after_handle(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked right after the actual service has been invoked, regardless
        of whether the service raised an exception or not.
        """

    def finalize_handle(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Offers the last chance to influence the service's operations.
        """

    @staticmethod
    def after_add_to_store(logger): # type: ignore
        """ Invoked right after the class has been added to the service store.
        """

    def validate_input(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked right before handle. Any exception raised means handle will not be called.
        """

    def validate_output(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked right after handle. Any exception raised means further hooks will not be called.
        """

    def get_request_hash(self, _zato_no_op_marker=zato_no_op_marker, *args, **kwargs): # type: ignore
        """ Lets services compute an incoming request's hash to decide whether i is already kept in cache,
        if one is configured for this request's channel.
        """

# ################################################################################################################################

    def _log_input_output(self, user_msg:'str', level:'int', suppress_keys:'strlist', is_response:'bool') -> 'stranydict':

        suppress_keys = suppress_keys or []
        suppressed_msg = '(suppressed)'
        container = 'response' if is_response else 'request'
        payload_key = '{}.payload'.format(container)
        user_msg = '{} '.format(user_msg) if user_msg else user_msg

        msg = {}
        if payload_key not in suppress_keys:
            msg[payload_key] = getattr(self, container).payload
        else:
            msg[payload_key] = suppressed_msg

        attrs = ('channel', 'cid', 'data_format', 'environ', 'impl_name',
                 'invocation_time', 'job_type', 'name', 'slow_threshold', 'usage', 'wsgi_environ')

        if is_response:
            attrs += ('handle_return_time', 'processing_time', 'processing_time_raw',
                      'zato.http.response.headers')

        for attr in attrs:
            if attr not in suppress_keys:
                msg[attr] = self.channel.type if attr == 'channel' else getattr(self, attr, '(None)')
            else:
                msg[attr] = suppressed_msg

        self.logger.log(level, '{}{}'.format(user_msg, msg))

        return msg

    def log_input(self, user_msg:'str'='', level:'int'=logging.INFO, suppress_keys:'any_'=None) -> 'stranydict':
        return self._log_input_output(user_msg, level, suppress_keys, False)

    def log_output(self, user_msg:'str'='', level:'int'=logging.INFO, suppress_keys:'any_'=('wsgi_environ',)) -> 'stranydict':
        return self._log_input_output(user_msg, level, suppress_keys, True)

# ################################################################################################################################

    @staticmethod
    def update(
        service,               # type: Service
        channel_type,          # type: str
        server,                # type: ParallelServer
        broker_client,         # type: BrokerClient
        _ignored,              # type: any_
        cid,                   # type: str
        payload,               # type: any_
        raw_request,           # type: any_
        transport='',          # type: str
        simple_io_config=None, # type: anydictnone
        data_format='',        # type: str
        wsgi_environ=None,     # type: dictnone
        job_type='',           # type: str
        channel_params=None,   # type: dictnone
        merge_channel_params=True, # type: bool
        params_priority='',        # type: str
        in_reply_to='',        # type: str
        environ=None,          # type: dictnone
        init=True,             # type: bool
        wmq_ctx=None,          # type: dictnone
        channel_info=None,     # type: ChannelInfo | None
        channel_item=None,     # type: dictnone
        wsx=None,              # type: WebSocket | None
        _AMQP=CHANNEL.AMQP,            # type: str
        _IBM_MQ=CHANNEL.IBM_MQ,        # type: str
        _HL7v2=HL7.Const.Version.v2.id # type: str
    ) -> 'None':
        """ Takes a service instance and updates it with the current request's context data.
        """
        wsgi_environ = wsgi_environ or {}

        service.server = server
        service.broker_client = broker_client
        service.cid = cid
        service.request.payload = payload
        service.request.raw_request = raw_request
        service.transport = transport
        service.data_format = data_format
        service.wsgi_environ = wsgi_environ or {}
        service.job_type = job_type
        service.translate = server.kvdb.translate # type: ignore
        service.user_config = server.user_config
        service.static_config = server.static_config
        service.time = server.time_util

        if channel_params:
            service.request.channel_params.update(channel_params)

        service.request.merge_channel_params = merge_channel_params
        service.in_reply_to = in_reply_to
        service.environ = environ or {}

        channel_item = wsgi_environ.get('zato.channel_item') or {}
        channel_item = cast_('stranydict', channel_item)
        sec_def_info = wsgi_environ.get('zato.sec_def', {})

        if channel_type == _AMQP:
            service.request.amqp = AMQPRequestData(channel_item['amqp_msg'])

        elif channel_type == _IBM_MQ:
            service.request.wmq = service.request.ibm_mq = IBMMQRequestData(wmq_ctx)

        elif data_format == _HL7v2:
            service.request.hl7 = HL7RequestData(channel_item['hl7_mllp_conn_ctx'], payload)

        chan_sec_info = ChannelSecurityInfo(
            sec_def_info.get('id'),
            sec_def_info.get('name'),
            sec_def_info.get('type'),
            sec_def_info.get('username'),
            sec_def_info.get('impl')
        )

        service.channel = service.chan = channel_info or ChannelInfo(
            channel_item.get('id'),
            channel_item.get('name'),
            channel_type,
            channel_item.get('data_format'),
            channel_item.get('is_internal'),
            channel_item.get('match_target'),
            chan_sec_info, channel_item
        )

        if init:
            service._init(channel_type in _wsgi_channels)

# ################################################################################################################################

    def new_instance(self, service_name:'str', *args:'any_', **kwargs:'any_') -> 'Service':
        """ Creates a new service instance without invoking its handle method.
        """
        service: 'Service'
        service, _ = \
            self.server.service_store.new_instance_by_name(service_name, *args, **kwargs)

        service.update(service, CHANNEL.NEW_INSTANCE, self.server, broker_client=self.broker_client, _ignored=None,
            cid=self.cid, payload=self.request.payload, raw_request=self.request.raw_request, wsgi_environ=self.wsgi_environ)

        return service

# ################################################################################################################################

class QuantumService(Service, metaclass=ABCMeta):
    """ 
    A base class for all quantum services deployed on Zato servers for defining quantum services in Zato. \n
    All its subclasses must implement at least the :mod:`circuit` and :mod:`after_circuit_execution` methods. \n
    If synchronization logic it's needed before or after the circuit execution, it can be specified in the methods :mod:`before_circuit_execution` and :mod:`after_circuit_exectuion`respectively.
    """

    circuit_result : 'GateModelQuantumTaskResult | AnnealingQuantumTaskResult | PhotonicModelQuantumTaskResult'

    def get_key(self):
        """ 
        Returns key from zato security vault using the specified name that will be used to authenticate to the cloud provider. \n
        By default it will be used 'default'
        """
        name = getattr(self.__class__, 'key_name', 'default')

        with closing(self.odb.session()) as session:
            query = session.query(
            SecurityBase.id,
            SecurityBase.name,
            SecurityBase.username,
            SecurityBase.password).\
            filter(SecurityBase.cluster_id==self.server.cluster_id).\
            filter(SecurityBase.name==name)
        
        key = query.one()

        return key.username, self.server.decrypt(key.password)

    def get_runs(self) -> 'int':
        """ 
        Returns the number of runs that will be used for excuting the quantum task. 
        By default it will be used 1000 runs
        """
        if(self.runs == None):
            runs = getattr(self.__class__, 'runs', 1000)
        else:
            runs = self.runs
        return runs
    
    # ################################################################################################################################
    
    def get_quantum_computer_name(self) -> 'str':
        """ Returns the quantum computer name that will be used for excuting the quantum task. 
        """

        if(self.quantum_computer == None):
            computer = getattr(self.__class__, 'quantum_computer', '')
        else:
            computer = self.quantum_computer

        return computer
    
    # ################################################################################################################################
    
    def get_timeout(self) -> 'int':
        """ Returns the timeout time in seconds that will be used for excuting the quantum task. 
        By default it will be 5 days in seconds
        """

        if(self.timeout == None):
            timeout = getattr(self.__class__, 'timeout', 5*24*60*60)
        else:
            timeout = self.timeout

        return timeout
    
    # ################################################################################################################################
    
    def get_confidence_threshold(self) -> 'float | None':
        """ Returns the confidence threshold that will be expected from excuting the quantum task. 
        By default it will be set to None and will not be taken in consideration for the execution of the task.
        """
        if(self.confidence_threshold == None):
            threshold = getattr(self.__class__, 'threshold', None)
        else:
            threshold = self.confidence_threshold
            

        return threshold
    
    # ################################################################################################################################
    
    def __init__(self, *ignored_args: any_, **ignored_kwargs: any_) -> None:
        super().__init__(*ignored_args, **ignored_kwargs)
        self.circuit_result = None
        self.key = None
        self.runs = None
        self.quantum_computer = None
        self.timeout = None
        self.confidence_threshold = None
    
    
    # ################################################################################################################################

    def invoke(self, zato_name:'any_',runs=None, key=None, quantum_computer=None, timeout=None, confidence_threshold=None, *args:'any_', **kwargs:'any_') -> 'any_':
        """ Invokes a service synchronously by its name.
        """
        # The 'zato_name' parameter is actually a service class,
        # not its name, and we need to extract the name ourselves.

        self.runs = runs
        self.key = key
        self.quantum_computer = quantum_computer
        self.timeout = timeout
        self.confidence_threshold = confidence_threshold

        if isclass(zato_name) and issubclass(zato_name, Service): # type: Service
            zato_name = zato_name.get_name()

        if self.component_enabled_target_matcher:
            zato_name, target = self.extract_target(zato_name) # type: ignore
            kwargs['target'] = target

        if self._enforce_service_invokes and self.invokes:
            if zato_name not in self.invokes:
                msg = 'Could not invoke `{}` which is not in `{}`'.format(zato_name, self.invokes)
                self.logger.warning(msg)
                raise ValueError(msg)

        return self.invoke_by_impl_name(self.server.service_store.name_to_impl_name[zato_name], *args, **kwargs)

    # ################################################################################################################################

    def invoke_async(
        self,
        name,       # type: str
        payload='', # type: str
        channel=CHANNEL.INVOKE_ASYNC, # type: str
        data_format=DATA_FORMAT.DICT, # type: str
        transport='', # type: str
        expiration=BROKER.DEFAULT_EXPIRATION, # type: int
        to_json_string=False, # type: bool
        cid='',        # type: str
        callback=None, # type: str | Service | None
        zato_ctx=None, # type: stranydict | None
        environ=None,   # type: stranydict | None
        runs=None,      # type: int | None
        key=None,       # type: str | None
        quantum_computer=None, # type: str | None
        timeout=None,    # type: str | None
        confidence_threshold=None # type: float | None

    ) -> 'str':
        """ Invokes a service asynchronously by its name.
        """

        self.runs = runs
        self.key = key
        self.quantum_computer = quantum_computer
        self.timeout = timeout
        self.confidence_threshold = confidence_threshold

        zato_ctx = zato_ctx if zato_ctx is not None else {}
        environ = environ if environ is not None else {}

        if self.component_enabled_target_matcher:
            name, target = self.extract_target(name)
            zato_ctx['zato.request_ctx.target'] = target
        else:
            target = None

        # Let's first find out if the service can be invoked at all
        impl_name = self.server.service_store.name_to_impl_name[name]

        if self.component_enabled_invoke_matcher:
            if not self._worker_store.invoke_matcher.is_allowed(impl_name):
                raise ZatoException(self.cid, 'Service `{}` (impl_name) cannot be invoked'.format(impl_name))

        if to_json_string:
            payload = dumps(payload)

        cid = cid or new_cid()

        # If there is any callback at all, we need to figure out its name because that's how it will be invoked by.
        if callback:

            # The same service
            if callback is self:
                callback = self.name

        else:
            sink = '{}-async-callback'.format(self.name)
            if sink in self.server.service_store.name_to_impl_name:
                callback = sink

            else:
                # Otherwise the callback must be a string pointing to the actual service to reply to
                # so we do not need to do anything.
                pass

        async_ctx = AsyncCtx()
        async_ctx.calling_service = self.name
        async_ctx.service_name = name
        async_ctx.cid = cid
        async_ctx.data = payload
        async_ctx.data_format = data_format
        async_ctx.zato_ctx = zato_ctx
        async_ctx.environ = environ

        if callback:
            async_ctx.callback = list(callback) if isinstance(callback, (list, tuple)) else [callback]

        spawn_greenlet(self._invoke_async, async_ctx, channel)

        return cid
    
    # ################################################################################################################################
        
    @abstractmethod
    def circuit(self) -> 'QuantumCircuit | Circuit':
        """ The method that describes the quantum circuit that will be excecuted. It must return the circuit definition"""
        raise NotImplementedError('Should be overridden by subclasses (QuantumService.circuit)')
    
    # ################################################################################################################################
    
    def before_circuit_execution(self, _zato_no_op_marker=zato_no_op_marker, *args, **kwargs): # type: ignore
        """ Invoked just before the actual service executes the quantum circuit.
        """

    # ################################################################################################################################
        
    @abstractmethod
    def after_circuit_execution(self, _zato_no_op_marker=zato_no_op_marker, *args, **kwargs): # type: ignore
        """ Invoked just after the actual service executes the quantum circuit. \n
        The implementation of this method is required \n
        The result of the circuit execution it's in :mod:`self.circuit_result`
        """
        raise NotImplementedError('Should be overridden by subclasses (QuantumService.after_circuit_execution)')

# ################################################################################################################################

class AWSQuantumService(QuantumService):
    """ 
    A base class for all quantum services deployed on Zato servers that uses AWS and Braket. \n
    All its subclasses must implement at least the :mod:`circuit` and :mod:`after_circuit_execution` methods. \n
    If synchronization logic it's needed before or after the circuit execution, it can be specified in the methods :mod:`before_circuit_execution` and :mod:`after_circuit_exectuion`respectively.
    """

    circuit_result : 'GateModelQuantumTaskResult | AnnealingQuantumTaskResult | PhotonicModelQuantumTaskResult'

    @classmethod
    def get_aws_region(class_:'type[AWSQuantumService]') -> 'str':
        """ Returns the AWS Region that will be used for excuting the quantum task. 
        By default it will be used us-east-1.
        """
        region = getattr(class_, 'aws_region', 'us-east-1')

        return region
    
    # ################################################################################################################################

    def handle(self) -> None:
        if self.before_circuit_execution:
            self.before_circuit_execution()
        
        circuit = self.circuit()
        key_id, access_key = self.get_key()
        session = Session(aws_access_key_id=key_id, aws_secret_access_key=access_key, region_name=self.get_aws_region())
        aws_session = AwsSession(boto_session=session)
        if(self.get_quantum_computer_name() == 'LocalSimulator'):
            device = LocalSimulator()
        else:
            device = AwsDevice(self.get_quantum_computer_name(), aws_session=aws_session)

        if(self.get_quantum_computer_name() == 'LocalSimulator'):
            task = device.run(circuit, shots=self.get_runs())
        else:
            task = device.run(circuit, shots=self.get_runs(), poll_timeout_seconds=self.get_timeout())
        result = task.result()
        if (result == None):
            msg = 'The task did not complete correctly or timed out, name:[{}]'.format(self.name)
            self.logger.error(msg)
            raise ZatoException(self.cid, msg)
        
        threshold_reached = False

        if self.get_confidence_threshold() != None:
            for probabilities in result.measurement_probabilities.values():
                if (probabilities > self.get_confidence_threshold()):
                    threshold_reached = True
            
            if not threshold_reached:
                msg = 'The task did not pass the specified confidence threshold, name:[{}]'.format(self.name)
                self.logger.error(msg)
                raise ZatoException(self.cid, msg)
        
        self.circuit_result = result
        self.after_circuit_execution()

    # ################################################################################################################################

    def circuit(self) -> 'Circuit':
        """ The method that describes the quantum circuit that will be excecuted. It must return the circuit definition"""
        raise NotImplementedError('Should be overridden by subclasses (AWSQuantumService.circuit)')
      
# ################################################################################################################################

class IBMQuantumService(QuantumService):
    """ 
    A base class for all quantum services deployed on Zato servers that uses IBM and Quiskit. \n
    All its subclasses must implement at least the :mod:`circuit` and :mod:`after_circuit_execution` methods. \n
    If synchronization logic it's needed before or after the circuit execution, it can be specified in the methods :mod:`before_circuit_execution` and :mod:`after_circuit_exectuion`respectively.
    """

    circuit_result : 'Result'
    
    # ################################################################################################################################

    def handle(self) -> None:
        if self.before_circuit_execution:
            self.before_circuit_execution()
        
        circuit = self.circuit()
        key_id, api_key = self.get_key()
        provider = IBMProvider(token=api_key)
        try:
            backend = provider.get_backend(self.get_quantum_computer_name())
        except QiskitBackendNotFoundError:
            msg = 'The computer introduced does not exists, name:[{}]'.format(self.name)
            self.logger.error(msg)
            raise ZatoException(self.cid, msg)
        
        transpiled_circuit = transpile(circuit, backend=backend)
        job = backend.run(transpiled_circuit, shots=self.get_runs(), memory=True)
        try:
            result = job.result(timeout=self.get_timeout())
        except IBMJobFailureError:
            msg = 'The task did not complete correctly, name:[{}]'.format(self.name)
            self.logger.error(msg)
            raise ZatoException(self.cid, msg)
        except IBMJobTimeoutError:
            msg = 'The task did timed out, name:[{}]'.format(self.name)
            self.logger.error(msg)
            raise ZatoException(self.cid, msg)
        
        threshold_reached = False

        if self.get_confidence_threshold() != None:
            for count in result.get_counts().values():
                if ((count/self.get_runs()) > self.get_confidence_threshold()):
                    threshold_reached = True
            
            if not threshold_reached:
                msg = 'The task did not pass the specified confidence threshold, name:[{}]'.format(self.name)
                self.logger.error(msg)
                raise ZatoException(self.cid, msg)
        
        self.circuit_result = result
        self.after_circuit_execution()

    # ################################################################################################################################

    def circuit(self) -> 'QuantumCircuit':
        """ The method that describes the quantum circuit that will be excecuted. It must return the circuit definition"""
        raise NotImplementedError('Should be overridden by subclasses (IBMQuantumService.circuit)')
      
# ################################################################################################################################

class _Hook(Service):
    """ Base class for all hook services.
    """
    _hook_func_name: 'stranydict'

    class SimpleIO:
        input_required = (Opaque('ctx'),)
        output_optional = ('hook_action',)

    def handle(self):
        func_name = self._hook_func_name[self.request.input.ctx.hook_type]
        func = getattr(self, func_name)
        func()

# ################################################################################################################################

class PubSubHook(_Hook):
    """ Subclasses of this class may act as pub/sub hooks.
    """
    _hook_func_name = {}

    def before_publish(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked for each pub/sub message before it is published to a topic.
        """

    def before_delivery(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked for each pub/sub message right before it is delivered to an endpoint.
        """

    def on_outgoing_soap_invoke(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked for each message that is to be sent through outgoing a SOAP Suds connection.
        """

    def on_subscribed(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked for each new topic subscription.
        """

    def on_unsubscribed(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked each time a client unsubscribes.
        """

PubSubHook._hook_func_name[PUBSUB.HOOK_TYPE.BEFORE_PUBLISH] = 'before_publish'
PubSubHook._hook_func_name[PUBSUB.HOOK_TYPE.BEFORE_DELIVERY] = 'before_delivery'
PubSubHook._hook_func_name[PUBSUB.HOOK_TYPE.ON_OUTGOING_SOAP_INVOKE] = 'on_outgoing_soap_invoke'
PubSubHook._hook_func_name[PUBSUB.HOOK_TYPE.ON_SUBSCRIBED] = 'on_subscribed'
PubSubHook._hook_func_name[PUBSUB.HOOK_TYPE.ON_UNSUBSCRIBED] = 'on_unsubscribed'

# ################################################################################################################################

class WSXHook(_Hook):
    """ Subclasses of this class may act as WebSockets hooks.
    """
    _hook_func_name = {}

    def on_connected(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked each time a new WSX connection is established.
        """

    def on_disconnected(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked each time an existing WSX connection is dropped.
        """

    def on_pubsub_response(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked each time a response to a previous pub/sub message arrives.
        """

    def on_vault_mount_point_needed(self, _zato_no_op_marker=zato_no_op_marker): # type: ignore
        """ Invoked each time there is need to discover the name of a Vault mount point
        that a particular WSX channel is secured ultimately with, i.e. the mount point
        where the incoming user's credentials are stored in.
        """

WSXHook._hook_func_name[WEB_SOCKET.HOOK_TYPE.ON_CONNECTED] = 'on_connected'
WSXHook._hook_func_name[WEB_SOCKET.HOOK_TYPE.ON_DISCONNECTED] = 'on_disconnected'
WSXHook._hook_func_name[WEB_SOCKET.HOOK_TYPE.ON_PUBSUB_RESPONSE] = 'on_pubsub_response'
WSXHook._hook_func_name[WEB_SOCKET.HOOK_TYPE.ON_VAULT_MOUNT_POINT_NEEDED] = 'on_vault_mount_point_needed'

# ################################################################################################################################
