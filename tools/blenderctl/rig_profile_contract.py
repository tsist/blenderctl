# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON Schema for unevaluated profiles; diagnostics intentionally preserve detail."""
def obj(properties,required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}
S={'type':'string'}
N={'type':['number','null']}
V={'type':'array','items':N,'minItems':3,'maxItems':3}
M={'type':'array','items':{'type':'array','items':N,'minItems':4,'maxItems':4},'minItems':4,'maxItems':4}
ANY={'type':'object'}
ARRAY={'type':'array','items':ANY}
BONE=obj({'name':S,'parent':{'type':['string','null']},'use_deform':{'type':'boolean'},'role':{'enum':['deform','non_deform_unspecified']},'head':V,'tail':V,'length':N,'rest_matrix':M,'axes':obj({'x':V,'y':V,'z':V}),'connected':{'type':'boolean'},'inherit_scale':S,'constraints':ARRAY})
RIG=obj({'object':S,'data':S,'linked':{'type':'boolean'},'bone_count':{'type':'integer','minimum':0},'deform_bone_count':{'type':'integer','minimum':0},'scale':V,'world_matrix':M,'bones':{'type':'array','items':BONE},'constraints':ARRAY,'animation':ANY,'data_animation':ANY,'ik_fk_semantics':S})
REPORT=obj({'version':{'const':'1.0'},'mode':{'const':'UNEVALUATED_READ_ONLY'},'rigs':{'type':'array','items':RIG},'actions':ARRAY,'mesh_weights':ARRAY,'diagnostics':obj({'missing_references':ARRAY,'non_finite':ARRAY,'structural_cycles':{'type':'array','items':{'type':'array','items':S}}}),'limits':{'type':'array','items':S}})
