# -*- coding: utf-8 -*-

"""
Copyright (C) 2017, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# Django
from django import forms

# Zato
from zato.admin.web.forms import add_select, add_services
from zato.common import PUBSUB

class MsgForm(forms.Form):
    correl_id = forms.CharField(widget=forms.TextInput(attrs={'style':'width:100%'}))
    in_reply_to = forms.CharField(widget=forms.TextInput(attrs={'style':'width:100%'}))
    expiration = forms.CharField(widget=forms.TextInput(attrs={'class':'required', 'style':'width:50%'}))
    priority = forms.CharField(widget=forms.TextInput(attrs={'class':'required', 'style':'width:30%'}), initial=5)
    mime_type = forms.CharField(widget=forms.HiddenInput())

class MsgPublishForm(MsgForm):
    topic_name = forms.ChoiceField(widget=forms.Select())
    publisher_id = forms.ChoiceField(widget=forms.Select())
    gd = forms.ChoiceField(widget=forms.Select())
    ext_client_id = forms.CharField(widget=forms.TextInput(attrs={'style':'width:100%'}))
    group_id = forms.CharField(widget=forms.TextInput(attrs={'style':'width:100%'}))
    msg_id = forms.CharField(widget=forms.TextInput(attrs={'style':'width:100%'}))
    position_in_group = forms.CharField(widget=forms.TextInput(attrs={'class':'required', 'style':'width:30%'}))
    hook_service_id = forms.ChoiceField(widget=forms.Select())

    def __init__(self, req, topic_name, topic_list, hook_service_name, publisher_list, *args, **kwargs):
        super(MsgPublishForm, self).__init__(*args, **kwargs)
        add_select(self, 'topic_name', topic_list)
        add_select(self, 'publisher_id', publisher_list)
        add_select(self, 'gd', PUBSUB.GD_CHOICE, False)
        add_services(self, req, initial_service=hook_service_name)

        self.initial['topic_name'] = topic_name
