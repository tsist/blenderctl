# SPDX-License-Identifier: GPL-3.0-or-later
"""Paired zone edits; socket identifiers survive item rename and reordering."""
from scene_contract import obj,enum,array,integer,NAME
ITEM=obj({'name':NAME,'type':enum('GEOMETRY','FLOAT','VECTOR')})
IDENTIFIER={'type':'string','pattern':'^Item_[0-9]+$'}
def operations(tree):
    result={}
    def add(name,props,required=None):result[name]=obj({'op':{'const':name},'tree':tree,**props},['op','tree',*(props if required is None else required)])
    add('zone.create',{'kind':enum('REPEAT','SIMULATION'),'input':NAME,'output':NAME,'items':array(ITEM,1,8),'iterations':integer(1,1000)},['kind','input','output','items'])
    add('zone.item.add',{'output':NAME,'item':ITEM})
    add('zone.item.rename',{'output':NAME,'identifier':IDENTIFIER,'name':NAME})
    add('zone.item.remove',{'output':NAME,'identifier':IDENTIFIER})
    add('zone.item.move',{'output':NAME,'identifier':IDENTIFIER,'index':integer(0,7)})
    add('zone.iterations',{'input':NAME,'value':integer(1,1000)})
    add('zone.remove',{'input':NAME,'output':NAME})
    return result
