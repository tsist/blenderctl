# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in native simple-driver profile; not permission to execute Python."""
from node_contract import obj,array,integer,NAME,SHA,validate

PROFILE=obj({'mode':{'const':'NATIVE_SIMPLE_V1'},'source_sha256':SHA,'inventory_sha256':SHA,
             'driver_count':integer(0,4096),'acknowledged_unresolved':{**array(SHA,0,4096),'uniqueItems':True}})
AUDIT=obj({'version':{'const':'1.0'},'source_sha256':SHA,'inventory_sha256':SHA,
           'driver_count':integer(0,4096),'drivers':array({'type':'object'},0,4096),
           'profile_template':PROFILE,'scope':{'type':'string'}})

def request_params(params):
    params['properties']['driver_profile']=PROFILE
    params.setdefault('allOf',[]).append({'if':{'required':['driver_profile']},'then':{'required':['expected_sha256']}})
    return params

def normalize(value):
    validate(value,PROFILE)
    return value
