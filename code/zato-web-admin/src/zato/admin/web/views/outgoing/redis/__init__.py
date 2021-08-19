# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# Django
from django.http import HttpResponse, HttpResponseServerError
from django.template.response import TemplateResponse

# Zato
from zato.admin.web.forms import SearchForm
from zato.admin.web.forms.kvdb import CreateForm, EditForm, RemoteCommandForm
from zato.admin.web.views import CreateEdit, Index as _Index, method_allowed
from zato.common.exception import ZatoException
from zato.common.json_internal import dumps
from zato.common.model.kvdb import KVDB as KVDBModel

# ################################################################################################################################
# ################################################################################################################################

class Index(_Index):
    method_allowed = 'GET'
    url_name = 'kvdb'
    template = 'zato/outgoing/redis/index.html'
    service_name = 'kvdb1.get-list'
    output_class = KVDBModel
    paginate = True

    class SimpleIO(_Index.SimpleIO):
        input_required = 'cluster_id',
        output_required = 'name', 'is_active', 'host', 'port', 'db', 'use_redis_sentinels', 'redis_sentinels', \
            'redis_sentinels_master'
        output_optional = 'id',
        output_repeated = True

    def handle(self):
        return {
            'create_form': CreateForm(),
            'edit_form': EditForm(prefix='edit'),
            'cluster_id': self.input.cluster_id,
        }

# ################################################################################################################################

class _CreateEdit(CreateEdit):
    method_allowed = 'POST'

    class SimpleIO(CreateEdit.SimpleIO):
        input_required = ('name', 'get_info', 'ip_mode', 'connect_timeout', 'auto_bind', 'server_list', 'pool_size',
            'pool_exhaust_timeout', 'pool_keep_alive', 'pool_max_cycles', 'pool_lifetime', 'pool_ha_strategy', 'username',
            'auth_type')
        input_optional = ('is_active', 'use_tls', 'pool_name', 'use_sasl_external', 'is_read_only', 'is_stats_enabled',
            'should_check_names', 'use_auto_range', 'should_return_empty_attrs', 'is_tls_enabled', 'tls_private_key_file',
            'tls_cert_file', 'tls_ca_certs_file', 'tls_version', 'tls_ciphers', 'tls_validate', 'sasl_mechanism')
        output_required = ('id', 'name')

    def success_message(self, item):
        return 'Successfully {} outgoing Redis connection `{}`'.format(self.verb, item.name)

# ################################################################################################################################

class Create(_CreateEdit):
    url_name = 'out-redis-create'
    service_name = 'kvdb1.create'

# ################################################################################################################################

class Edit(_CreateEdit):
    url_name = 'out-redis-edit'
    form_prefix = 'edit-'
    service_name = 'kvdb1.edit'

# ################################################################################################################################
# ################################################################################################################################

@method_allowed('GET')
def remote_command(req):

    return_data = {'form':RemoteCommandForm(),
                   'cluster':req.zato.get('cluster'),
                   'search_form':SearchForm(req.zato.clusters, req.GET),
                   'zato_clusters':req.zato.clusters,
                   'cluster_id':req.zato.cluster_id,
                   }

    return TemplateResponse(req, 'zato/kvdb/remote-command.html', return_data)

# ################################################################################################################################
# ################################################################################################################################

@method_allowed('POST')
def remote_command_execute(req):
    """ Executes a command against the key/value DB.
    """
    try:
        response = req.zato.client.invoke('zato.kvdb.remote-command.execute', {'command': req.POST['command']})
        if response.has_data:
            return HttpResponse(dumps({'message': dumps(response.data.result)}), content_type='application/javascript')
        else:
            raise ZatoException(msg=response.details)
    except Exception as e:
        return HttpResponseServerError(e.args[0])

# ################################################################################################################################
# ################################################################################################################################
