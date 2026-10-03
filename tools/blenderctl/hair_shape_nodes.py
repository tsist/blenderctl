# SPDX-License-Identifier: GPL-3.0-or-later
"""Live triangle-frame binding, expressed entirely as native Geometry Nodes."""
def definition(surface):
    nodes={'Input':('NodeGroupInput',{},{}),'Output':('NodeGroupOutput',{},{}),
        'Object':('GeometryNodeObjectInfo',{'transform_space':'RELATIVE'},{0:surface,1:False}),
        'Position':('GeometryNodeInputPosition',{},{}),
        'Set':('GeometryNodeSetPosition',{},{1:True,3:[0,0,0]}),
        'Weights':('GeometryNodeInputNamedAttribute',{'data_type':'FLOAT_VECTOR'},{0:'groom_weights'}),
        'W':('ShaderNodeSeparateXYZ',{},{}),
        'Offset':('GeometryNodeInputNamedAttribute',{'data_type':'FLOAT_VECTOR'},{0:'groom_offset'}),
        'O':('ShaderNodeSeparateXYZ',{}, {})}
    # Radius stays on the original native curve data; it is not recomputed or
    # multiplied by the GN surface transform. Root ID is likewise preserved.
    links=[('Input',0,'Set',0),('Set',0,'Output',0),('Weights',0,'W',0),('Offset',0,'O',0)]
    for i,k in enumerate('ABC'):
        nodes['Index'+k]=('GeometryNodeInputNamedAttribute',{'data_type':'INT'},{0:'groom_v'+str(i)})
        nodes[k]=('GeometryNodeSampleIndex',{'data_type':'FLOAT_VECTOR','domain':'POINT','clamp':False},{})
        nodes['Weight'+k]=('ShaderNodeVectorMath',{'operation':'SCALE'},{1:[0,0,0],2:[0,0,0]})
        links.extend([('Object',4,k,0),('Position',0,k,1),('Index'+k,0,k,2),(k,0,'Weight'+k,0),('W',i,'Weight'+k,3)])
    def vector(name,operation):nodes[name]=('ShaderNodeVectorMath',{'operation':operation},{2:[0,0,0],3:1.0})
    for name,operation in [('AB','SUBTRACT'),('AC','SUBTRACT'),('T','NORMALIZE'),('Cross','CROSS_PRODUCT'),('N','NORMALIZE'),('Bitan','CROSS_PRODUCT'),('RootAB','ADD'),('Root','ADD'),('AlongT','SCALE'),('AlongB','SCALE'),('AlongN','SCALE'),('AlongTB','ADD'),('Along','ADD'),('Result','ADD')]:vector(name,operation)
    links.extend([('B',0,'AB',0),('A',0,'AB',1),('C',0,'AC',0),('A',0,'AC',1),('AB',0,'T',0),('AB',0,'Cross',0),('AC',0,'Cross',1),('Cross',0,'N',0),('N',0,'Bitan',0),('T',0,'Bitan',1),('WeightA',0,'RootAB',0),('WeightB',0,'RootAB',1),('RootAB',0,'Root',0),('WeightC',0,'Root',1),('T',0,'AlongT',0),('Bitan',0,'AlongB',0),('N',0,'AlongN',0),('O',0,'AlongT',3),('O',1,'AlongB',3),('O',2,'AlongN',3),('AlongT',0,'AlongTB',0),('AlongB',0,'AlongTB',1),('AlongTB',0,'Along',0),('AlongN',0,'Along',1),('Root',0,'Result',0),('Along',0,'Result',1),('Result',0,'Set',2)])
    for name in ['AlongT','AlongB','AlongN']:nodes[name][2].pop(3)
    return nodes,links
