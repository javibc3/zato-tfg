# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from traceback import format_exc

# rapidjson
from rapidjson import dumps

# Python 2/3 compatibility
from future.utils import itervalues

# Zato
from zato.common.api import CHANNEL, ContentType, CONTENT_TYPE, PUBSUB, ZATO_NONE
from zato.common.exception import BadRequest, Forbidden, PubSubSubscriptionExists
from zato.common.util.auth import parse_basic_auth
from zato.server.service import AsIs, Int, Service
from zato.server.service.internal.pubsub.subscription import CreateWSXSubscription

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from zato.common.typing_ import anylist

# ################################################################################################################################
# ################################################################################################################################

class BaseSIO:
    input_required = ('topic_name',)
    response_elem = None
    skip_empty_keys = True
    default_value = None

# ################################################################################################################################

class TopicSIO(BaseSIO):
    input_optional = ('data', AsIs('msg_id'), 'has_gd', Int('priority'),
        Int('expiration'), 'mime_type', AsIs('correl_id'), 'in_reply_to', AsIs('ext_client_id'), 'ext_pub_time',
        'sub_key')
    output_optional = AsIs('msg_id')

# ################################################################################################################################

class SubSIO(BaseSIO):
    input_optional  = 'sub_key', 'delivery_method'
    output_optional = 'sub_key', Int('queue_depth')

# ################################################################################################################################

class _PubSubService(Service):

    def _pubsub_check_credentials(self, _invoke_channels=(CHANNEL.INVOKE, CHANNEL.INVOKE_ASYNC)):

        # If we are being through a CHANNEL.INVOKE* channel, it means that our caller used self.invoke
        # or self.invoke_async, so there will never by any credentials in HTTP headers (there is no HTTP request after all),
        # and we can run as an internal endpoint in this situation.
        if self.channel.type in _invoke_channels:
            return self.server.default_internal_pubsub_endpoint_id

        auth = self.wsgi_environ.get('HTTP_AUTHORIZATION')
        if not auth:
            raise Forbidden(self.cid)

        try:
            username, password = parse_basic_auth(auth)
        except ValueError:
            raise Forbidden(self.cid)

        basic_auth = itervalues(self.server.worker_store.request_dispatcher.url_data.basic_auth_config)

        for item in basic_auth:
            config = item['config']
            if config['is_active']:
                if config['username'] == username and config['password'] == password:
                    auth_ok = True
                    security_id = config['id']
                    break
                else:
                    auth_ok = False

        if not auth_ok:
            raise Forbidden(self.cid)

        try:
            endpoint_id = self.pubsub.get_endpoint_id_by_sec_id(security_id)
        except KeyError:
            self.logger.warning('Client credentials are valid but there is no pub/sub endpoint using them, sec_id:`%s`, e:`%s`',
                security_id, format_exc())
            raise Forbidden(self.cid)
        else:
            return endpoint_id

# ################################################################################################################################

class TopicService(_PubSubService):
    """ Main service responsible for publications to and deliveries from a given topic. Handles security and distribution
    of messages to target queues or recipients.
    """
    SimpleIO = TopicSIO

# ################################################################################################################################

    def _publish(self, endpoint_id):
        """ POST /zato/pubsub/topic/{topic_name} {"data":"my data", ...}
        """
        # We always require some data on input
        if not self.request.input.data:
            raise BadRequest(self.cid, 'No data sent on input')

        # Ignore the header set by curl and similar tools
        mime_type = self.wsgi_environ.get('CONTENT_TYPE')
        mime_type = mime_type if mime_type != ContentType.FormURLEncoded else CONTENT_TYPE.JSON

        input = self.request.input

        ctx = {
            'mime_type': mime_type,
            'data': input.data,
            'priority': input.priority,
            'expiration': input.expiration,
            'correl_id': input.correl_id,
            'in_reply_to': input.in_reply_to,
            'ext_client_id': input.ext_client_id,
            'has_gd': input.has_gd or ZATO_NONE,
            'endpoint_id': endpoint_id,
        }

        return self.pubsub.publish(input.topic_name, **ctx)

# ################################################################################################################################

    def _get_messages(self, endpoint_id:'int') -> 'anylist':
        """ POST /zato/pubsub/topic/{topic_name}
        """

        # Local aliases
        topic_name = self.request.input.topic_name

        # Not every channel may present a sub_key on input
        if self.chan.type in (CHANNEL.WEB_SOCKET, CHANNEL.SERVICE): # type: ignore
            sub_key = self.request.input.get('sub_key')
        else:
            sub = self.pubsub.get_subscription_by_endpoint_id(endpoint_id, topic_name, needs_error=False)
            if sub:
                sub_key = sub.sub_key
            else:
                raise BadRequest(self.cid, 'You are not subscribed to topic `{}`'.format(topic_name), needs_msg=True)

        try:
            self.pubsub.get_subscription_by_sub_key(sub_key)
        except KeyError:
            self.logger.warning('Could not find sub_key:`%s`, e:`%s`', sub_key, format_exc())
            raise Forbidden(self.cid)
        else:
            return self.pubsub.get_messages(topic_name, sub_key, needs_msg_id=True)

# ################################################################################################################################

    def handle_POST(self):

        # Checks credentials and returns endpoint_id if valid
        endpoint_id = self._pubsub_check_credentials()

        # Extracts payload and publishes the message
        self.response.payload.msg_id = self._publish(endpoint_id)

# ################################################################################################################################

    def handle_PATCH(self):

        # Checks credentials and returns endpoint_id if valid
        endpoint_id = self._pubsub_check_credentials()

        # Find our messages ..
        messages = self._get_messages(endpoint_id)

        # .. and return them to the caller.
        self.response.payload = dumps(messages)

# ################################################################################################################################

class SubscribeService(_PubSubService):
    """ Service through which REST clients subscribe to or unsubscribe from topics.
    """
    SimpleIO = SubSIO

# ################################################################################################################################

    def _check_sub_access(self, endpoint_id:'int') -> 'None':

        # At this point we know that the credentials are valid and in principle, there is such an endpoint,
        # but we still don't know if it has permissions to subscribe to this topic and we don't want to reveal
        # information about what topics exist or not.
        try:
            topic = self.pubsub.get_topic_by_name(self.request.input.topic_name)
        except KeyError:
            self.logger.warning(format_exc())
            raise Forbidden(self.cid)

        # We know the topic exists but we also need to make sure the endpoint can subscribe to it
        if not self.pubsub.is_allowed_sub_topic_by_endpoint_id(topic.name, endpoint_id):
            endpoint = self.pubsub.get_endpoint_by_id(endpoint_id)
            self.logger.warning('Endpoint `%s` is not allowed to subscribe to `%s`', endpoint.name, self.request.input.topic_name)
            raise Forbidden(self.cid)

# ################################################################################################################################

    def handle_POST(self):
        """ POST /zato/pubsub/subscribe/topic/{topic_name}
        """
        # Checks credentials and returns endpoint_id if valid
        endpoint_id = self._pubsub_check_credentials()

        # Make sure this endpoint has correct subscribe permissions (patterns)
        self._check_sub_access(endpoint_id)

        try:
            response = self.invoke('zato.pubsub.subscription.subscribe-rest', {
                'topic_name': self.request.input.topic_name,
                'endpoint_id': endpoint_id,
                'delivery_batch_size': PUBSUB.DEFAULT.DELIVERY_BATCH_SIZE,
                'delivery_method': self.request.input.delivery_method or PUBSUB.DELIVERY_METHOD.PULL.id,
                'server_id': self.server.id,
            })['response']
        except PubSubSubscriptionExists:
            msg = 'Subscription for topic `%s` already exists for endpoint_id `%s`'
            self.logger.info(msg, self.request.input.topic_name, endpoint_id)
        else:
            self.response.payload.sub_key     = response['sub_key']
            self.response.payload.queue_depth = response['queue_depth']

# ################################################################################################################################

    def _handle_DELETE(self):
        """ Low-level implementation of DELETE /zato/pubsub/subscribe/topic/{topic_name}
        """
        # Local aliases
        topic_name = self.request.input.topic_name

        # This may be provided by WebSockets
        sub_key = self.request.input.get('sub_key')

        # Not every channel may present a sub_key on input
        if sub_key and self.chan.type not in (CHANNEL.WEB_SOCKET, CHANNEL.SERVICE): # type: ignore
            self.logger.warning('Channel type `%s` may not use sub_key on input (%s)', self.chan.type, sub_key)
            raise Forbidden(self.cid)

        # Checks credentials and returns endpoint_id if valid
        endpoint_id = self._pubsub_check_credentials()

        if not endpoint_id:
            self.logger.warning('Could not find endpoint for input credentials')
            return

        # To unsubscribe, we also need to have the right subscription permissions first (patterns) ..
        self._check_sub_access(endpoint_id)

        # .. also check that sub_key exists and that we are not using another endpoint's sub_key.
        try:
            if sub_key:
                sub = self.pubsub.get_subscription_by_sub_key(sub_key)
            else:
                sub = self.pubsub.get_subscription_by_endpoint_id(endpoint_id, topic_name, needs_error=False)
        except KeyError:
            self.logger.warning('Could not find subscription by endpoint_id:`%s`, endpoint:`%s`',
                endpoint_id, self.pubsub.get_endpoint_by_id(endpoint_id).name)
            raise Forbidden(self.cid)
        else:
            if not sub:
                self.logger.info('No subscription for sub_key: `%s` and endpoint_id: `%s` (%s) (delete)',
                    sub_key, endpoint_id, topic_name)
                return

            # Raise an exception if current endpoint is not the one that created the subscription originally,
            # but only if current endpoint is not the default internal one; in such a case we want to let
            # the call succeed - this lets other services use self.invoke in order to unsubscribe.
            if sub.endpoint_id != endpoint_id:
                if endpoint_id != self.server.default_internal_pubsub_endpoint_id:
                    sub_endpoint = self.pubsub.get_endpoint_by_id(sub.endpoint_id)
                    self_endpoint = self.pubsub.get_endpoint_by_id(endpoint_id)
                    self.logger.warning('Endpoint `%s` cannot unsubscribe sk:`%s` (%s) created by `%s`',
                        self_endpoint.name,
                        sub.sub_key,
                        self.pubsub.get_topic_by_sub_key(sub.sub_key).name,
                        sub_endpoint.name)
                    raise Forbidden(self.cid)

            # We have all permissions checked now and can proceed to the actual calls
            response = self.invoke('zato.pubsub.endpoint.delete-endpoint-queue', {
                'cluster_id': self.server.cluster_id,
                'sub_key': sub.sub_key
            })

            # Make sure that we always return JSON payload
            response = response or {}

            # Assign the response ..
            self.response.payload = response

            # .. and clean up WSX state if the caller was a WebSocket.
            if sub.is_wsx:
                self.invoke('zato.channel.web-socket.client.unregister-ws-sub-key', {
                    'sub_key_list': [sub.sub_key],
                })

# ################################################################################################################################

    def handle_DELETE(self):
        """ DELETE /zato/pubsub/subscribe/topic/{topic_name}
        """
        # Call our implementation ..
        self._handle_DELETE()

        # .. and always return an empty response.
        self.response.payload = {}

# ################################################################################################################################

class PublishMessage(Service):
    """ Lets one publish messages to a topic.
    """
    SimpleIO = TopicSIO

    def handle(self):
        response = self.invoke(TopicService.get_name(), self.request.input, wsgi_environ={'REQUEST_METHOD':'POST'})
        self.response.payload = response

# ################################################################################################################################

class GetMessages(Service):
    """ Used to return outstanding messages from a topic.
    """
    SimpleIO = TopicSIO

    def handle(self):
        response = self.invoke(TopicService.get_name(), self.request.input, wsgi_environ={'REQUEST_METHOD':'PATCH'})
        self.response.payload = response

# ################################################################################################################################

class Subscribe(Service):
    """ Lets callers subscribe to topics.
    """
    SimpleIO = SubSIO

    def handle(self):
        response = self.invoke(SubscribeService.get_name(), self.request.input, wsgi_environ={'REQUEST_METHOD':'POST'})
        self.response.payload = response

# ################################################################################################################################

# Added for completness so as to make WSX clients use services from this module only
class SubscribeWSX(CreateWSXSubscription):
    """ An alias to CreateWSXSubscription, added for API completeness.
    """
    name = 'zato.pubsub.pubapi.subscribe-wsx'

# ################################################################################################################################

class Unsubscribe(Service):
    """ Lets one unsubscribe from a topic.
    """
    SimpleIO = SubSIO

    def handle(self):
        response = self.invoke(SubscribeService.get_name(), self.request.input, wsgi_environ={'REQUEST_METHOD':'DELETE'})
        self.response.payload = response

# ################################################################################################################################
