# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded AST arithmetic; never eval/exec or Python attribute resolution.

Intervals conservatively reject unprovable domains, including Blender's silent
overflow-to-zero case. Transform variables use a conservative spatial bound.
"""
import ast,math,operator
from protocol import Failure
LIMIT=3.4028234663852886e38
def interval(*values):
    if not values or not all(math.isfinite(v) and abs(v)<=LIMIT for v in values):
        raise Failure('VALIDATION_FAILED','Native driver arithmetic exceeds finite float range')
    return min(values),max(values)
def binary(op,a,b):
    if isinstance(op,(ast.Div,ast.FloorDiv,ast.Mod)) and b[0]<=0<=b[1]:raise Failure('VALIDATION_FAILED','Driver denominator interval includes zero')
    fn={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.FloorDiv:operator.floordiv,ast.Pow:operator.pow}.get(type(op))
    if isinstance(op,ast.Mod):return interval(-max(abs(v) for v in b),max(abs(v) for v in b))
    if not fn:raise Failure('UNSUPPORTED','Driver arithmetic operator is outside numeric profile')
    if isinstance(op,ast.Pow) and (a[0]<0 or b[0]!=b[1] or abs(b[0])>32):raise Failure('UNSUPPORTED','Driver power domain is outside numeric profile')
    return interval(*(fn(x,y) for x in a for y in b))
def prove(expression,variables,frame):
    try:tree=ast.parse(expression,mode='eval')
    except (SyntaxError,ValueError,RecursionError) as exc:raise Failure('UNSUPPORTED','Cannot parse native expression for numeric validation') from exc
    if sum(1 for _ in ast.walk(tree))>256:raise Failure('UNSUPPORTED','Native expression exceeds 256 AST nodes')
    names={'frame':interval(frame),'pi':interval(math.pi),'e':interval(math.e),'True':(1,1),'False':(0,0),**variables}
    def walk(n,depth=0):
        if depth>32:raise Failure('UNSUPPORTED','Native expression exceeds numeric recursion bound')
        child=lambda x:walk(x,depth+1)
        if isinstance(n,ast.Constant) and type(n.value) in (int,float,bool):return interval(n.value)
        if isinstance(n,ast.Name) and n.id in names:return names[n.id]
        if isinstance(n,ast.BinOp):return binary(n.op,child(n.left),child(n.right))
        if isinstance(n,ast.UnaryOp):
            a=child(n.operand)
            if isinstance(n.op,ast.USub):return interval(-a[1],-a[0])
            if isinstance(n.op,ast.UAdd):return a
            if isinstance(n.op,ast.Not):return (1,1) if a==(0,0) else (0,0) if a[0]>0 or a[1]<0 else (0,1)
        if isinstance(n,ast.Compare):
            a=child(n.left);out=True
            for op,r in zip(n.ops,n.comparators):
                b=child(r)
                if a[0]!=a[1] or b[0]!=b[1]:return (0,1)
                fn={ast.Lt:operator.lt,ast.LtE:operator.le,ast.Gt:operator.gt,ast.GtE:operator.ge,ast.Eq:operator.eq,ast.NotEq:operator.ne}.get(type(op))
                if fn is None:raise Failure('UNSUPPORTED','Unsupported driver comparison')
                out=out and fn(a[0],b[0]);a=b
            return interval(int(out))
        if isinstance(n,ast.BoolOp):
            values=[child(x) for x in n.values]
            # Python boolean operators return an operand, not necessarily 0/1.
            return interval(*(v for pair in values for v in pair))
        if isinstance(n,ast.IfExp):
            c=child(n.test)
            if c==(0,0):return child(n.orelse)
            if c[0]>0 or c[1]<0:return child(n.body)
            return interval(*child(n.body),*child(n.orelse))
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and not n.keywords:
            name=n.func.id;a=[child(x) for x in n.args]
            if name in ('min','max') and a:
                fn=min if name=='min' else max;return interval(fn(x[0] for x in a),fn(x[1] for x in a))
            if name=='abs' and len(a)==1:return interval(0 if a[0][0]<=0<=a[0][1] else min(abs(x) for x in a[0]),max(abs(x) for x in a[0]))
            if name in ('sin','cos') and len(a)==1:return (-1,1)
            if name=='lerp' and len(a)==3:return binary(ast.Add(),a[0],binary(ast.Mult(),a[2],binary(ast.Sub(),a[1],a[0])))
            if name=='smoothstep' and len(a)==3:
                binary(ast.Div(),binary(ast.Sub(),a[2],a[0]),binary(ast.Sub(),a[1],a[0]));return (0,1)
            if name=='atan2' and len(a)==2:return (-math.pi,math.pi)
            if name=='pow' and len(a)==2:return binary(ast.Pow(),*a)
            if name in ('sqrt','exp','log','acos','asin','atan','floor','ceil','degrees','radians') and len(a)==1:
                lo,hi=a[0]
                if name in ('acos','asin') and (lo<-1 or hi>1):raise ValueError('Inverse trig domain')
                fn=getattr(math,name);return interval(fn(lo),fn(hi))
        raise Failure('UNSUPPORTED','Native expression is outside bounded numeric profile')
    try:return walk(tree.body)
    except (ValueError,OverflowError,ZeroDivisionError,TypeError) as exc:raise Failure('VALIDATION_FAILED','Native driver arithmetic domain or overflow failure') from exc
