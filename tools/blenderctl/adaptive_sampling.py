# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, witness-based adaptive analysis; never a continuous-time certificate."""
import heapq
import numpy as np


class BudgetReached(Exception):
    pass


def analyze(start, end, settings, sample):
    """sample(t) returns (vertices[N,3], actual_frame); owns persistence/budgets."""
    accepted=[];pending=[];history=[];stop=None;serial=0
    tolerance=settings['max_position_error']

    def interval(a,b,depth):
        va,ta=sample(a);vb,tb=sample(b);checks=[]
        for alpha in (.25,.5,.75):
            t=a+(b-a)*alpha;v,actual=sample(t)
            error=float(np.linalg.norm(v-(va*(1-alpha)+vb*alpha),axis=1).max())
            checks.append({'requested_frame':t,'actual_frame':actual,'alpha':alpha,'error':error})
        row={'start':a,'end':b,'actual_start':ta,'actual_end':tb,'depth':depth,
             'max_error':max(c['error'] for c in checks),'checks':checks}
        history.append(row)
        return row

    def enqueue(row):
        nonlocal serial
        if row['max_error']<=tolerance:accepted.append(row)
        else:
            serial+=1;heapq.heappush(pending,(-row['max_error'],serial,row))

    count=max(1,int(np.ceil((end-start)*settings['seed_substeps'])))
    seeds=[start+(end-start)*i/count for i in range(count+1)]
    unexamined=[]
    for a,b in zip(seeds,seeds[1:]):
        try:enqueue(interval(a,b,0))
        except BudgetReached as exc:
            stop=str(exc);unexamined=[{'start':a,'end':end}];break
    while pending and stop is None:
        _,_,row=heapq.heappop(pending)
        a,b=row['start'],row['end'];mid=(a+b)/2
        if not any(row['actual_start']<c['actual_frame']<row['actual_end'] for c in row['checks']):
            stop='native_time_resolution'
        elif row['depth']>=settings['max_depth']:stop='max_depth'
        elif not a<mid<b:stop='requested_time_resolution'
        if stop:
            enqueue(row);break
        # Keep the parent unresolved unless BOTH child checks complete.
        try:left=interval(a,mid,row['depth']+1);right=interval(mid,b,row['depth']+1)
        except BudgetReached as exc:
            stop=str(exc);enqueue(row);break
        enqueue(left);enqueue(right)
    unresolved=sorted([r for _,_,r in pending],key=lambda r:r['start'])
    accepted.sort(key=lambda r:r['start'])
    return {'outcome':'needs_review' if stop else 'sampled_intervals_pass','stop_reason':stop,
            'accepted_intervals':accepted,'unresolved_intervals':unresolved,'unexamined_ranges':unexamined,
            'interval_history':history,'key_frames':sorted({v for r in accepted+unresolved for v in (r['start'],r['end'])}),
            'tolerance':tolerance,'continuous_time_verified':False,'exchange_verified':False}
