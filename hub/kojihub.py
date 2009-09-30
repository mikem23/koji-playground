# Python library

# kojihub - library for koji's XMLRPC interface
# Copyright (c) 2005-2007 Red Hat
#
#    Koji is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; 
#    version 2.1 of the License.
#
#    This software is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this software; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
# Authors:
#       Mike McLean <mikem@redhat.com>
#       Cristian Balint <cbalint@redhat.com>

import base64
import calendar
import koji
import koji.auth
import koji.db
import koji.policy
import datetime
import errno
import logging
import fcntl
import fnmatch
from koji.util import md5_constructor
import os
import random
import re
import rpm
import stat
import subprocess
import sys
import tempfile
import time
import types
import xmlrpclib
from koji.context import context


logger = logging.getLogger('koji.hub')

def log_error(msg):
    if hasattr(context,'req'):
        context.req.log_error(msg)
    else:
        sys.stderr.write(msg + "\n")
    logger.error(msg)


class Task(object):
    """A task for the build hosts"""

    fields = (
                ('task.id', 'id'),
                ('task.state', 'state'),
                ('task.create_time', 'create_time'),
                ('EXTRACT(EPOCH FROM create_time)','create_ts'),
                ('task.completion_time', 'completion_time'),
                ('EXTRACT(EPOCH FROM completion_time)','completion_ts'),
                ('task.channel_id', 'channel_id'),
                ('task.host_id', 'host_id'),
                ('task.parent', 'parent'),
                ('task.label', 'label'),
                ('task.waiting', 'waiting'),
                ('task.awaited', 'awaited'),
                ('task.owner', 'owner'),
                ('task.method', 'method'),
                ('task.arch', 'arch'),
                ('task.priority', 'priority'),
                ('task.weight', 'weight'))

    def __init__(self,id):
        self.id = id
        self.logger = logging.getLogger("koji.hub.Task")

    def verifyHost(self,host_id=None):
        """Verify that host owns task"""
        if host_id is None:
            host_id = context.session.host_id
        if host_id is None:
            return False
        task_id = self.id
        #getting a row lock on this task to ensure task assignment sanity
        #no other concurrent transaction should be altering this row
        q = """SELECT state,host_id FROM task WHERE id=%(task_id)s FOR UPDATE"""
        r = _fetchSingle(q, locals())
        if not r:
            raise koji.GenericError, "No such task: %i" % task_id
        state, otherhost = r
        return (state == koji.TASK_STATES['OPEN'] and otherhost == host_id)

    def assertHost(self,host_id):
        if not self.verifyHost(host_id):
            raise koji.ActionNotAllowed, "host %d does not own task %d" % (host_id,self.id)

    def getOwner(self):
        """Return the owner (user_id) for this task"""
        q = """SELECT owner FROM task WHERE id=%(id)i"""
        return _singleValue(q, vars(self))

    def verifyOwner(self,user_id=None):
        """Verify that user owns task"""
        if user_id is None:
            user_id = context.session.user_id
        if user_id is None:
            return False
        task_id = self.id
        #getting a row lock on this task to ensure task state sanity
        q = """SELECT owner FROM task WHERE id=%(task_id)s FOR UPDATE"""
        r = _fetchSingle(q, locals())
        if not r:
            raise koji.GenericError, "No such task: %i" % task_id
        (owner,) = r
        return (owner == user_id)

    def assertOwner(self,user_id=None):
        if not self.verifyOwner(user_id):
            raise koji.ActionNotAllowed, "user %d does not own task %d" % (user_id,self.id)

    def lock(self,host_id,newstate='OPEN',force=False):
        """Attempt to associate the task for host, either to assign or open

        returns True if successful, False otherwise"""
        #we use row-level locks to keep things sane
        #note the SELECT...FOR UPDATE
        task_id = self.id
        if not force:
            q = """SELECT state,host_id FROM task WHERE id=%(task_id)i FOR UPDATE"""
            r = _fetchSingle(q,locals())
            if not r:
                raise koji.GenericError, "No such task: %i" % task_id
            state, otherhost = r
            if state == koji.TASK_STATES['FREE']:
                if otherhost is not None:
                    log_error("Error: task %i is both free and locked (host %i)"
                        % (task_id,otherhost))
                    return False
            elif state == koji.TASK_STATES['ASSIGNED']:
                if otherhost is None:
                    log_error("Error: task %i is assigned, but has no assignee"
                        % (task_id))
                    return False
                elif otherhost != host_id:
                    #task is assigned to someone else
                    return False
                #otherwise the task is assigned to host_id, so keep going
            else:
                if otherhost is None:
                    log_error("Error: task %i is non-free but unlocked (state %i)"
                        % (task_id,state))
                return False
        #if we reach here, task is either
        #  - free and unlocked
        #  - assigned to host_id
        #  - force option is enabled
        state = koji.TASK_STATES[newstate]
        q = """UPDATE task SET state=%(state)s,host_id=%(host_id)s
        WHERE id=%(task_id)s"""
        _dml(q,locals())
        return True

    def assign(self,host_id,force=False):
        """Attempt to assign the task to host.

        returns True if successful, False otherwise"""
        return self.lock(host_id,'ASSIGNED',force)

    def open(self,host_id):
        """Attempt to open the task for host.

        returns task data if successful, None otherwise"""
        if self.lock(host_id,'OPEN'):
            # get more complete data to return
            fields = self.fields + (('task.request', 'request'),)
            q = """SELECT %s FROM task WHERE id=%%(id)i""" % ','.join([f[0] for f in fields])
            ret = _singleRow(q, vars(self), [f[1] for f in fields], strict=True)
            if ret['request'].find('<?xml', 0, 10) == -1:
                #handle older base64 encoded data
                ret['request'] = base64.decodestring(ret['request'])
            return ret
        else:
            return None

    def free(self):
        """Free a task"""
        task_id = self.id
        # access checks should be performed by calling function
        query = """SELECT state FROM task WHERE id = %(id)i FOR UPDATE"""
        row = _fetchSingle(query,vars(self))
        if not row:
            raise koji.GenericError, "No such task: %i" % self.id
        oldstate = row[0]
        if koji.TASK_STATES[oldstate] in ['CLOSED','CANCELED','FAILED']:
            raise koji.GenericError, "Cannot free task %i, state is %s" % \
                    (self.id,koji.TASK_STATES[oldstate])
        newstate = koji.TASK_STATES['FREE']
        newhost = None
        q = """UPDATE task SET state=%(newstate)s,host_id=%(newhost)s
        WHERE id=%(task_id)s"""
        _dml(q,locals())
        return True

    def setWeight(self,weight):
        """Set weight for task"""
        task_id = self.id
        # access checks should be performed by calling function
        q = """UPDATE task SET weight=%(weight)s WHERE id = %(task_id)s"""
        _dml(q,locals())

    def setPriority(self, priority, recurse=False):
        """Set priority for task"""
        task_id = self.id
        priority = int(priority)

        # access checks should be performed by calling function
        q = """UPDATE task SET priority=%(priority)s WHERE id = %(task_id)s"""
        _dml(q,locals())

        if recurse:
            """Change priority of child tasks"""
            q = """SELECT id FROM task WHERE parent = %(task_id)s"""
            for (child_id,) in _fetchMulti(q, locals()):
                Task(child_id).setPriority(priority, recurse=True)

    def _close(self,result,state):
        """Mark task closed and set response

        Returns True if successful, False if not"""
        task_id = self.id
        # access checks should be performed by calling function
        st_closed = koji.TASK_STATES['CLOSED']
        update = """UPDATE task SET result = %(result)s, state = %(state)s, completion_time = NOW()
        WHERE id = %(task_id)d
        """
        _dml(update,locals())

    def close(self,result):
        # access checks should be performed by calling function
        self._close(result,koji.TASK_STATES['CLOSED'])

    def fail(self,result):
        # access checks should be performed by calling function
        self._close(result,koji.TASK_STATES['FAILED'])

    def getState(self):
        query = """SELECT state FROM task WHERE id = %(id)i"""
        return _singleValue(query, vars(self))

    def isFinished(self):
        return (koji.TASK_STATES[self.getState()] in ['CLOSED','CANCELED','FAILED'])

    def isCanceled(self):
        return (self.getState() == koji.TASK_STATES['CANCELED'])

    def isFailed(self):
        return (self.getState() == koji.TASK_STATES['FAILED'])

    def cancel(self,recurse=True):
        """Cancel this task.

        A task can only be canceled if it is not already in the 'CLOSED' state.
        If it is, no action will be taken.  Return True if the task is
        successfully canceled, or if it was already canceled, False if it is
        closed."""
        # access checks should be performed by calling function
        task_id = self.id
        q = """SELECT state FROM task WHERE id = %(task_id)s FOR UPDATE"""
        state = _singleValue(q,locals())
        st_canceled = koji.TASK_STATES['CANCELED']
        st_closed = koji.TASK_STATES['CLOSED']
        st_failed = koji.TASK_STATES['FAILED']
        if state == st_canceled:
            return True
        elif state in [st_closed,st_failed]:
            return False
        update = """UPDATE task SET state = %(st_canceled)i, completion_time = NOW()
        WHERE id = %(task_id)i"""
        _dml(update, locals())
        #cancel associated builds (only if state is 'BUILDING')
        #since we check build state, we avoid loops with cancel_build on our end
        b_building = koji.BUILD_STATES['BUILDING']
        q = """SELECT id FROM build WHERE task_id = %(task_id)i
        AND state = %(b_building)i
        FOR UPDATE"""
        for (build_id,) in _fetchMulti(q, locals()):
            cancel_build(build_id, cancel_task=False)
        if recurse:
            #also cancel child tasks
            self.cancelChildren()
        return True

    def cancelChildren(self):
        """Cancel child tasks"""
        task_id = self.id
        q = """SELECT id FROM task WHERE parent = %(task_id)i"""
        for (id,) in _fetchMulti(q,locals()):
            Task(id).cancel(recurse=True)

    def cancelFull(self,strict=True):
        """Cancel this task and every other task in its group

        If strict is true, then this must be a top-level task
        Otherwise we will follow up the chain to find the top-level task
        """
        task_id = self.id
        q = """SELECT parent FROM task WHERE id = %(task_id)i FOR UPDATE"""
        parent = _singleValue(q,locals())
        if parent is not None:
            if strict:
                raise koji.GenericError, "Task %d is not top-level (parent=%d)" % (task_id,parent)
            #otherwise, find the top-level task and go from there
            seen = {task_id:1}
            while parent is not None:
                if seen.has_key(parent):
                    raise koji.GenericError, "Task LOOP at task %i" % task_id
                task_id = parent
                seen[task_id] = 1
                parent = _singleValue(q,locals())
            return Task(task_id).cancelFull(strict=True)
        #We handle the recursion ourselves, since self.cancel will stop at
        #canceled or closed tasks.
        tasklist = [task_id]
        seen = {}
        #query for use in loop
        q_children = """SELECT id FROM task WHERE parent = %(task_id)i"""
        for task_id in tasklist:
            if seen.has_key(task_id):
                #shouldn't happen
                raise koji.GenericError, "Task LOOP at task %i" % task_id
            seen[task_id] = 1
            Task(task_id).cancel(recurse=False)
            for (child_id,) in _fetchMulti(q_children,locals()):
                tasklist.append(child_id)

    def getRequest(self):
        id = self.id
        query = """SELECT request FROM task WHERE id = %(id)i"""
        xml_request = _singleValue(query, locals())
        if xml_request.find('<?xml', 0, 10) == -1:
            #handle older base64 encoded data
            xml_request = base64.decodestring(xml_request)
        params, method = xmlrpclib.loads(xml_request)
        return params

    def getResult(self):
        query = """SELECT state,result FROM task WHERE id = %(id)i"""
        r = _fetchSingle(query, vars(self))
        if not r:
            raise koji.GenericError, "No such task"
        state, xml_result = r
        if koji.TASK_STATES[state] == 'CANCELED':
            raise koji.GenericError, "Task %i is canceled" % self.id
        elif koji.TASK_STATES[state] not in ['CLOSED','FAILED']:
            raise koji.GenericError, "Task %i is not finished" % self.id
        # If the result is a Fault, then loads will raise it
        # This is probably what we want to happen.
        # Note that you can't really 'return' a fault over xmlrpc, you
        # can only 'raise' them.
        # If you try to return a fault as a value, it gets reduced to
        # a mere struct.
        # f = Fault(1,"hello"); print dumps((f,))
        if xml_result.find('<?xml', 0, 10) == -1:
            #handle older base64 encoded data
            xml_result = base64.decodestring(xml_result)
        result, method = xmlrpclib.loads(xml_result)
        return result[0]

    def getInfo(self, strict=True, request=False):
        """Return information about the task in a dictionary.  If "request" is True,
        the request will be decoded and included in the dictionary."""
        q = """SELECT %s FROM task WHERE id = %%(id)i""" % ','.join([f[0] for f in self.fields])
        result = _singleRow(q, vars(self), [f[1] for f in self.fields], strict)
        if request:
            result['request'] = self.getRequest()
        return result

    def getChildren(self, request=False):
        """Return information about tasks with this task as their
        parent.  If there are no such Tasks, return an empty list."""
        fields = self.fields
        if request:
            fields = fields + (('request', 'request'),)
        query = """SELECT %s FROM task WHERE parent = %%(id)i""" % ', '.join([f[0] for f in fields])
        results = _multiRow(query, vars(self), [f[1] for f in fields])
        if request:
            for task in results:
                if task['request'].find('<?xml', 0, 10) == -1:
                    #handle older base64 encoded data
                    task['request'] = base64.decodestring(task['request'])
                task['request'] = xmlrpclib.loads(task['request'])[0]
        return results

def make_task(method,arglist,**opts):
    """Create a task

    This call should not be directly exposed via xmlrpc
    Optional args:
        parent: the id of the parent task (creates a subtask)
        label: (subtasks only) the label of the subtask
        owner: the user_id that should own the task
        channel: the channel to place the task in
        arch: the arch for the task
        priority: the priority of the task
    """
    if opts.has_key('parent'):
        # for subtasks, we use some of the parent's options as defaults
        fields = ('state','owner','channel_id','priority','arch')
        q = """SELECT %s FROM task WHERE id = %%(parent)i""" % ','.join(fields)
        r = _fetchSingle(q,opts)
        if not r:
            raise koji.GenericError, "Invalid parent task: %(parent)s" % opts
        pdata = dict(zip(fields,r))
        if pdata['state'] != koji.TASK_STATES['OPEN']:
            raise koji.GenericError, "Parent task (id %(parent)s) is not open" % opts
        #default to a higher priority than parent
        opts.setdefault('priority', pdata['priority'] - 1)
        for f in ('owner','channel_id','arch'):
            opts.setdefault(f,pdata[f])
        opts.setdefault('label',None)
    else:
        opts.setdefault('priority',koji.PRIO_DEFAULT)
        #calling function should enforce priority limitations, if applicable
        opts.setdefault('arch','noarch')
        opts.setdefault('channel','default')
        #no labels for top-level tasks
        #calling function should enforce channel limitations, if applicable
        opts['channel_id'] = get_channel_id(opts['channel'],strict=True)
        if not context.session.logged_in:
            raise koji.GenericError, 'task must have an owner'
        else:
            opts['owner'] = context.session.user_id
        opts['label'] = None
        opts['parent'] = None
    #XXX - temporary workaround
    if method in ('buildArch', 'buildSRPMFromSCM') and opts['arch'] == 'noarch':
        #not all arches can generate a proper buildroot for all tags
        tag = get_tag(arglist[1])
        if not tag['arches']:
            raise koji.BuildError, 'no arches defined for tag %s' % tag['name']
        # canonicalize tagarches, since get_all_arches() is canonical but
        # non-canonical arches may be set in tag['arches']
        tagarches = [koji.canonArch(a) for a in tag['arches'].split()]
        for a in get_all_arches():
            if a not in tagarches:
                random.seed()
                opts['arch'] = random.choice(tagarches)
                break

    # encode xmlrpc request
    opts['request'] = xmlrpclib.dumps(tuple(arglist), methodname=method,
                                      allow_none=1)
    opts['state'] = koji.TASK_STATES['FREE']
    opts['method'] = method
    # stick it in the database
    q = """
    INSERT INTO task (state,owner,method,request,priority,
        parent,label,channel_id,arch)
    VALUES (%(state)s,%(owner)s,%(method)s,%(request)s,%(priority)s,
        %(parent)s,%(label)s,%(channel_id)s,%(arch)s);
    """
    _dml(q,opts)
    q = """SELECT currval('task_id_seq')"""
    task_id = _singleValue(q, {})
    return task_id

def mktask(__taskopts,__method,*args,**opts):
    """A wrapper around make_task with alternate signature

    Parameters:
        _taskopts: a dictionary of task options (e.g. priority, ...)
        _method: the method to be invoked

    All remaining args (incl. optional ones) are passed on to the task.
    """
    return make_task(__method,koji.encode_args(*args,**opts),**__taskopts)

def eventCondition(event, table=None):
    """return the proper WHERE condition to select data at the time specified by event. """
    if not table:
        table = ''
    else:
        table += '.'
    if event is None:
        return """(%(table)sactive = TRUE)""" % locals()
    elif isinstance(event, int) or isinstance(event, long):
        return """(%(table)screate_event <= %(event)d AND ( %(table)srevoke_event IS NULL OR %(event)d < %(table)srevoke_event ))""" \
            % locals()
    else:
        raise koji.GenericError, "Invalid event: %r" % event

def readGlobalInheritance(event=None):
    c=context.cnx.cursor()
    fields = ('tag_id','parent_id','name','priority','maxdepth','intransitive',
                'noconfig','pkg_filter')
    q="""SELECT %s FROM tag_inheritance JOIN tag ON parent_id = id
    WHERE %s
    ORDER BY priority
    """ % (",".join(fields), eventCondition(event))
    c.execute(q,locals())
    #convert list of lists into a list of dictionaries
    return [ dict(zip(fields,x)) for x in c.fetchall() ]

def readInheritanceData(tag_id,event=None):
    c=context.cnx.cursor()
    fields = ('parent_id','name','priority','maxdepth','intransitive','noconfig','pkg_filter')
    q="""SELECT %s FROM tag_inheritance JOIN tag ON parent_id = id
    WHERE %s AND tag_id = %%(tag_id)i
    ORDER BY priority
    """ % (",".join(fields), eventCondition(event))
    c.execute(q,locals())
    #convert list of lists into a list of dictionaries
    data = [ dict(zip(fields,x)) for x in c.fetchall() ]
    # include the current tag_id as child_id, so we can retrace the inheritance chain later
    for datum in data:
        datum['child_id'] = tag_id
    return data

def readDescendantsData(tag_id,event=None):
    c=context.cnx.cursor()
    fields = ('tag_id','parent_id','name','priority','maxdepth','intransitive','noconfig','pkg_filter')
    q="""SELECT %s FROM tag_inheritance JOIN tag ON tag_id = id
    WHERE %s AND parent_id = %%(tag_id)i
    ORDER BY priority
    """ % (",".join(fields), eventCondition(event))
    c.execute(q,locals())
    #convert list of lists into a list of dictionaries
    data = [ dict(zip(fields,x)) for x in c.fetchall() ]
    return data

def writeInheritanceData(tag_id, changes, clear=False):
    """Add or change inheritance data for a tag"""
    context.session.assertPerm('admin')
    fields = ('parent_id','priority','maxdepth','intransitive','noconfig','pkg_filter')
    if isinstance(changes,dict):
        changes = [changes]
    for link in changes:
        check_fields = fields
        if link.get('delete link'):
            check_fields = ('parent_id')
        for f in fields:
            if not link.has_key(f):
                raise koji.GenericError, "No value for %s" % f
    # read current data and index
    data = dict([[link['parent_id'],link] for link in readInheritanceData(tag_id)])
    for link in changes:
        link['is_update'] = True
        parent_id = link['parent_id']
        orig = data.get(parent_id)
        if link.get('delete link'):
            if orig:
                data[parent_id] = link
        elif not orig or clear:
            data[parent_id] = link
        else:
            #not a delete request and we have a previous link to parent
            for f in fields:
                if orig[f] != link[f]:
                    data[parent_id] = link
                    break
    if clear:
        for link in data.itervalues():
            if not link.get('is_update'):
                link['delete link'] = True
                link['is_update'] = True
    changed = False
    for link in data.itervalues():
        if link.get('is_update'):
            changed = True
            break
    if not changed:
        # nothing to do
        log_error("No inheritance changes")
        return
    #check for duplicate priorities
    pri_index = {}
    for link in data.itervalues():
        if link.get('delete link'):
            continue
        pri_index.setdefault(link['priority'], []).append(link)
    for pri, dups in pri_index.iteritems():
        if len(dups) <= 1:
            continue
        #oops, duplicate entries for a single priority
        dup_ids = [ link['parent_id'] for link in dups]
        raise koji.GenericError, "Inheritance priorities must be unique (pri %s: %r )" % (pri, dup_ids)
    # get an event
    event = _singleValue("SELECT get_event()")
    for parent_id, link in data.iteritems():
        if not link.get('is_update'):
            continue
        # revoke old values
        q = """
        UPDATE tag_inheritance SET active=NULL,revoke_event=%(event)s
        WHERE tag_id=%(tag_id)s AND parent_id = %(parent_id)s AND active = TRUE
        """
        _dml(q,locals())
    for parent_id, link in data.iteritems():
        if not link.get('is_update'):
            continue
        # skip rest if we are just deleting
        if link.get('delete link'):
            continue
        # insert new value
        newlink = {}
        for f in fields:
            newlink[f] = link[f]
        newlink['tag_id'] = tag_id
        newlink['create_event'] = event
        # defaults ok for the rest
        keys = newlink.keys()
        flist = ','.join(["%s" % k for k in keys])
        vlist = ','.join(["%%(%s)s" % k for k in keys])
        q = """
        INSERT INTO tag_inheritance (%(flist)s)
        VALUES (%(vlist)s)
        """ % locals()
        _dml(q,newlink)

def readFullInheritance(tag_id,event=None,reverse=False,stops={},jumps={}):
    """Returns a list representing the full, ordered inheritance from tag"""
    order = []
    readFullInheritanceRecurse(tag_id,event,order,stops,{},{},0,None,False,[],reverse,jumps)
    return order

def readFullInheritanceRecurse(tag_id,event,order,prunes,top,hist,currdepth,maxdepth,noconfig,pfilter,reverse,jumps):
    if maxdepth is not None and maxdepth < 1:
        return
    #note: maxdepth is relative to where we are, but currdepth is absolute from
    #the top.
    currdepth += 1
    top = top.copy()
    top[tag_id] = 1
    if reverse:
        node = readDescendantsData(tag_id,event)
    else:
        node = readInheritanceData(tag_id,event)
    for link in node:
        if reverse:
            id = link['tag_id']
        else:
            id = link['parent_id']
        if jumps.has_key(id):
            id = jumps[id]
        if top.has_key(id):
            #LOOP!
            log_error("Warning: INHERITANCE LOOP detected at %s -> %s, pruning" % (tag_id,id))
            #auto prune
            continue
        if prunes.has_key(id):
            # ignore pruned tags
            continue
        if link['intransitive'] and len(top) > 1:
            # ignore intransitive inheritance links, except at root
            continue
        if link['priority'] < 0:
            #negative priority indicates pruning, rather than inheritance
            prunes[id] = 1
            continue
        #propagate maxdepth
        nextdepth = link['maxdepth']
        if nextdepth is None:
            if maxdepth is not None:
                nextdepth = maxdepth - 1
        elif maxdepth is not None:
            nextdepth = min(nextdepth,maxdepth) - 1
        link['nextdepth'] = nextdepth
        link['currdepth'] = currdepth
        #propagate noconfig and pkg_filter controls
        if link['noconfig']:
            noconfig = True
        filter = list(pfilter)  # copy
        pattern = link['pkg_filter']
        if pattern:
            filter.append(pattern)
        link['filter'] = filter
        # check history to avoid redundant entries
        if hist.has_key(id):
            #already been there
            #BUT, options may have been different
            rescan = True
            #since rescans are possible, we might have to consider more than one previous hit
            for previous in hist[id]:
                sufficient = True       # is previous sufficient?
                # if last depth was less than current, then previous insufficient
                lastdepth = previous['nextdepth']
                if nextdepth is None:
                    if lastdepth is not None:
                        sufficient = False
                elif lastdepth is not None and lastdepth < nextdepth:
                    sufficient = False
                # if noconfig was on before, but not now, then insuffient
                if previous['noconfig'] and not noconfig:
                    sufficient = False
                # if we had a filter before, then insufficient
                if len(previous['filter']) > 0:
                    # FIXME - we could probably be a little more precise here
                    sufficient = False
                if sufficient:
                    rescan = False
            if not rescan:
                continue
        else:
            hist[id] = []
        hist[id].append(link)   #record history
        order.append(link)
        readFullInheritanceRecurse(id,event,order,prunes,top,hist,currdepth,nextdepth,noconfig,filter,reverse,jumps)

# tag-package operations
#       add
#       remove
#       block
#       unblock
#       change owner
#       list


def _pkglist_remove(tag_id,pkg_id,event_id=None):
    if event_id is None:
        event_id = _singleValue("SELECT get_event()")
    q = """UPDATE tag_packages SET active=NULL,revoke_event=%(event_id)i
    WHERE active = TRUE AND package_id=%(pkg_id)i AND tag_id=%(tag_id)i"""
    _dml(q,locals())

def _pkglist_add(tag_id,pkg_id,owner,block,extra_arches,event_id=None):
    if event_id is None:
        event_id = _singleValue("SELECT get_event()")
    #revoke old entry (if present)
    _pkglist_remove(tag_id,pkg_id,event_id)
    q = """INSERT INTO tag_packages(package_id,tag_id,owner,blocked,extra_arches,create_event)
    VALUES (%(pkg_id)s,%(tag_id)s,%(owner)s,%(block)s,%(extra_arches)s,%(event_id)s) """
    _dml(q,locals())

def pkglist_add(taginfo,pkginfo,owner=None,block=None,extra_arches=None,force=False,update=False):
    """Add to (or update) package list for tag"""
    #only admins....
    context.session.assertPerm('admin')
    tag = get_tag(taginfo, strict=True)
    pkg = lookup_package(pkginfo, create=True)
    tag_id = tag['id']
    pkg_id = pkg['id']
    if owner is not None:
        owner = get_user(owner,strict=True)['id']
    # first check to see if package is:
    #   already present (via inheritance)
    #   blocked
    pkglist = readPackageList(tag_id, pkgID=pkg_id, inherit=True)
    previous = pkglist.get(pkg_id,None)
    if previous is None:
        if block is None:
            block = False
        else:
            block = bool(block)
        if update and not force:
            #if update flag is true, require that there be a previous entry
            raise koji.GenericError, "cannot update: tag %s has no data for package %s" \
                    % (tag['name'],pkg['name'])
    else:
        #already there (possibly via inheritance)
        if owner is None:
            owner = previous['owner_id']
        if block is None:
            block = previous['blocked']
        else:
            block = bool(block)
        if extra_arches is None:
            extra_arches = previous['extra_arches']
        #see if the data is the same
        changed = False
        for key,value in (('owner_id',owner),
                          ('blocked',block),
                          ('extra_arches',extra_arches)):
            if previous[key] != value:
                changed = True
                break
        if not changed and not force:
            #no point in adding it again with the same data
            return
        if previous['blocked'] and not block and not force:
            raise koji.GenericError, "package %s is blocked in tag %s" % (pkg['name'],tag['name'])
    if owner is None:
        if force:
            owner = context.session.user_id
        else:
            raise koji.GenericError, "owner not specified"
    _pkglist_add(tag_id,pkg_id,owner,block,extra_arches)

def pkglist_remove(taginfo,pkginfo,force=False):
    """Remove package from the list for tag

    Most of the time you really want to use the block or unblock functions

    The main reason to remove an entry like this is to remove an override so
    that the package data can be inherited from elsewhere.
    """
    #only admins....
    context.session.assertPerm('admin')
    tag_id = get_tag_id(taginfo, strict=True)
    pkg_id = get_package_id(pkginfo, strict=True)
    _pkglist_remove(tag_id,pkg_id)

def pkglist_block(taginfo,pkginfo):
    """Block the package in tag"""
    pkglist_add(taginfo,pkginfo,block=True)

def pkglist_unblock(taginfo,pkginfo):
    """Unblock the package in tag

    Generally this just adds a unblocked duplicate of the blocked entry.
    However, if the block is actually in tag directly (not through inheritance),
    the blocking entry is simply removed"""
    tag = get_tag(taginfo, strict=True)
    pkg = lookup_package(pkginfo, strict=True)
    tag_id = tag['id']
    pkg_id = pkg['id']
    pkglist = readPackageList(tag_id, pkgID=pkg_id, inherit=True)
    previous = pkglist.get(pkg_id,None)
    if previous is None:
        raise koji.GenericError, "no data (blocked or otherwise) for package %s in tag %s" \
                % (pkg['name'],tag['name'])
    if not previous['blocked']:
        raise koji.GenericError, "package %s NOT blocked in tag %s" % (pkg['name'],tag['name'])
    event_id = _singleValue("SELECT get_event()")
    if previous['tag_id'] != tag_id:
        _pkglist_add(tag_id,pkg_id,previous['owner_id'],False,previous['extra_arches'])
    else:
        #just remove the blocking entry
        event_id = _singleValue("SELECT get_event()")
        _pkglist_remove(tag_id,pkg_id,event_id)
        #it's possible this was the only entry in the inheritance or that the next entry
        #back is also a blocked entry. if so, we need to add it back as unblocked
        pkglist = readPackageList(tag_id, pkgID=pkg_id, inherit=True)
        if not pkglist.has_key(pkg_id) or pkglist[pkg_id]['blocked']:
            _pkglist_add(tag_id,pkg_id,previous['owner_id'],False,previous['extra_arches'],
                         event_id)

def pkglist_setowner(taginfo,pkginfo,owner,force=False):
    """Set the owner for package in tag"""
    pkglist_add(taginfo,pkginfo,owner=owner,force=force,update=True)

def pkglist_setarches(taginfo,pkginfo,arches,force=False):
    """Set extra_arches for package in tag"""
    pkglist_add(taginfo,pkginfo,extra_arches=arches,force=force,update=True)

def readPackageList(tagID=None, userID=None, pkgID=None, event=None, inherit=False, with_dups=False):
    """Returns the package list for the specified tag or user.

    One of (tagID,userID,pkgID) must be specified

    Note that the returned data includes blocked entries
    """
    if tagID is None and userID is None and pkgID is None:
        raise koji.GenericError, 'tag,user, and/or pkg must be specified'

    packages = {}
    fields = (('package.id', 'package_id'), ('package.name', 'package_name'),
              ('tag.id', 'tag_id'), ('tag.name', 'tag_name'),
              ('users.id', 'owner_id'), ('users.name', 'owner_name'),
              ('extra_arches','extra_arches'),
              ('tag_packages.blocked', 'blocked'))
    flist = ', '.join([pair[0] for pair in fields])
    cond = eventCondition(event)
    q = """
    SELECT %(flist)s
    FROM tag_packages
    JOIN tag on tag.id = tag_packages.tag_id
    JOIN package ON package.id = tag_packages.package_id
    JOIN users ON users.id = tag_packages.owner
    WHERE %(cond)s"""
    if tagID != None:
        q += """
        AND tag.id = %%(tagID)i"""
    if userID != None:
        q += """
        AND users.id = %%(userID)i"""
    if pkgID != None:
        if isinstance(pkgID, int) or isinstance(pkgID, long):
            q += """
            AND package.id = %%(pkgID)i"""
        else:
            q += """
            AND package.name = %%(pkgID)s"""

    q = q % locals()
    for p in _multiRow(q, locals(), [pair[1] for pair in fields]):
        # things are simpler for the first tag
        pkgid = p['package_id']
        if with_dups:
            packages.setdefault(pkgid,[]).append(p)
        else:
            packages[pkgid] = p

    if tagID is None or (not inherit):
        return packages

    order = readFullInheritance(tagID, event)

    re_cache = {}
    for link in order:
        tagID = link['parent_id']
        filter = link['filter']
        # precompile filter patterns
        re_list = []
        for pat in filter:
            prog = re_cache.get(pat,None)
            if prog is None:
                prog = re.compile(pat)
                re_cache[pat] = prog
            re_list.append(prog)
        # same query as before, with different params
        for p in _multiRow(q, locals(), [pair[1] for pair in fields]):
            pkgid = p['package_id']
            if not with_dups and packages.has_key(pkgid):
                #previous data supercedes
                continue
            # apply package filters
            skip = False
            for prog in re_list:
                # the list of filters is cumulative, i.e.
                # the package name must match all of them
                if prog.match(p['package_name']) is None:
                    skip = True
                    break
            if skip:
                continue
            if with_dups:
                packages.setdefault(pkgid,[]).append(p)
            else:
                packages[pkgid] = p
    return packages


def readTaggedBuilds(tag,event=None,inherit=False,latest=False,package=None,owner=None):
    """Returns a list of builds for specified tag

    set inherit=True to follow inheritance
    set event to query at a time in the past
    set latest=True to get only the latest build per package
    """
    # build - id pkg_id version release epoch
    # tag_listing - id build_id tag_id

    taglist = [tag]
    if inherit:
        taglist += [link['parent_id'] for link in readFullInheritance(tag, event)]

    #regardless of inherit setting, we need to use inheritance to read the
    #package list
    packages = readPackageList(tagID=tag, event=event, inherit=True, pkgID=package)

    #these values are used for each iteration
    fields = (('tag.id', 'tag_id'), ('tag.name', 'tag_name'), ('build.id', 'id'),
              ('build.id', 'build_id'), ('build.version', 'version'), ('build.release', 'release'),
              ('build.epoch', 'epoch'), ('build.state', 'state'), ('build.completion_time', 'completion_time'),
              ('build.task_id','task_id'),
              ('events.id', 'creation_event_id'), ('events.time', 'creation_time'),
              ('package.id', 'package_id'), ('package.name', 'package_name'),
              ('package.name', 'name'),
              ("package.name || '-' || build.version || '-' || build.release", 'nvr'),
              ('users.id', 'owner_id'), ('users.name', 'owner_name'))
    st_complete = koji.BUILD_STATES['COMPLETE']

    q="""SELECT %s
    FROM tag_listing
    JOIN tag ON tag.id = tag_listing.tag_id
    JOIN build ON build.id = tag_listing.build_id
    JOIN users ON users.id = build.owner
    JOIN events ON events.id = build.create_event
    JOIN package ON package.id = build.pkg_id
    WHERE %s AND tag_id=%%(tagid)s
        AND build.state=%%(st_complete)i
    """ % (', '.join([pair[0] for pair in fields]), eventCondition(event, 'tag_listing'))
    if package:
        q += """AND package.name = %(package)s
        """
    if owner:
        q += """AND users.name = %(owner)s
        """
    q += """ORDER BY tag_listing.create_event DESC
    """
    # i.e. latest first

    builds = []
    seen = {}   # used to enforce the 'latest' option
    for tagid in taglist:
        #log_error(koji.db._quoteparams(q,locals()))
        for build in _multiRow(q, locals(), [pair[1] for pair in fields]):
            pkgid = build['package_id']
            pinfo = packages.get(pkgid,None)
            if pinfo is None or pinfo['blocked']:
                # note:
                # tools should endeavor to keep tag_listing sane w.r.t.
                # the package list, but if there is disagreement the package
                # list should take priority
                continue
            if latest:
                if seen.has_key(pkgid):
                    #only take the first (note ordering in query above)
                    continue
                seen[pkgid] = 1
            builds.append(build)

    return builds

def readTaggedRPMS(tag, package=None, arch=None, event=None,inherit=False,latest=True,rpmsigs=False,owner=None):
    """Returns a list of rpms for specified tag

    set inherit=True to follow inheritance
    set event to query at a time in the past
    set latest=False to get all tagged RPMS (not just from the latest builds)
    """
    taglist = [tag]
    if inherit:
        #XXX really should cache this - it gets called several places
        #   (however, it is fairly quick)
        taglist += [link['parent_id'] for link in readFullInheritance(tag, event)]

    builds = readTaggedBuilds(tag, event=event, inherit=inherit, latest=latest, package=package, owner=owner)
    #index builds
    build_idx = dict([(b['build_id'],b) for b in builds])

    #the following query is run for each tag in the inheritance
    fields = [('rpminfo.name', 'name'),
              ('rpminfo.version', 'version'),
              ('rpminfo.release', 'release'),
              ('rpminfo.arch', 'arch'),
              ('rpminfo.id', 'id'),
              ('rpminfo.epoch', 'epoch'),
              ('rpminfo.payloadhash', 'payloadhash'),
              ('rpminfo.size', 'size'),
              ('rpminfo.buildtime', 'buildtime'),
              ('rpminfo.buildroot_id', 'buildroot_id'),
              ('rpminfo.build_id', 'build_id')]
    if rpmsigs:
        fields.append(('rpmsigs.sigkey', 'sigkey'))
    q="""SELECT %s FROM rpminfo
    JOIN tag_listing ON rpminfo.build_id = tag_listing.build_id
    """ % ', '.join([pair[0] for pair in fields])
    if package:
        q += """JOIN build ON rpminfo.build_id = build.id
        JOIN package ON package.id = build.pkg_id
        """
    if rpmsigs:
        q += """LEFT OUTER JOIN rpmsigs on rpminfo.id = rpmsigs.rpm_id
        """
    q += """WHERE %s AND tag_id=%%(tagid)s
    """ % eventCondition(event)
    if package:
        q += """AND package.name = %(package)s
        """
    if arch:
        if isinstance(arch, basestring):
            q += """AND rpminfo.arch = %(arch)s
            """
        elif isinstance(arch, (list, tuple)):
            q += """AND rpminfo.arch IN %(arch)s\n"""
        else:
            raise koji.GenericError, 'invalid arch option: %s' % arch

    # unique constraints ensure that each of these queries will not report
    # duplicate rpminfo entries, BUT since we make the query multiple times,
    # we can get duplicates if a package is multiply tagged.
    rpms = []
    tags_seen = {}
    for tagid in taglist:
        if tags_seen.has_key(tagid):
            #certain inheritance trees can (legitimately) have the same tag
            #appear more than once (perhaps once with a package filter and once
            #without). The hard part of that was already done by readTaggedBuilds.
            #We only need consider each tag once. Note how we use build_idx below.
            #(Without this, we could report the same rpm twice)
            continue
        else:
            tags_seen[tagid] = 1
        for rpminfo in _multiRow(q, locals(), [pair[1] for pair in fields]):
            #note: we're checking against the build list because
            # it has been filtered by the package list. The tag
            # tools should endeavor to keep tag_listing sane w.r.t.
            # the package list, but if there is disagreement the package
            # list should take priority
            build = build_idx.get(rpminfo['build_id'],None)
            if build is None:
                continue
            elif build['tag_id'] != tagid:
                #wrong tag
                continue
            rpms.append(rpminfo)
    return [rpms,builds]

def check_tag_access(tag_id,user_id=None):
    """Determine if user has access to tag package with tag.

    Returns a tuple (access, override, reason)
        access: a boolean indicating whether access is allowed
        override: a boolean indicating whether access may be forced
        reason: the reason access is blocked
    """
    if user_id is None:
        user_id = context.session.user_id
    if user_id is None:
        raise koji.GenericError, "a user_id is required"
    perms = koji.auth.get_user_perms(user_id)
    override = False
    if 'admin' in perms:
        override = True
    tag = get_tag(tag_id)
    if tag['locked']:
        return (False, override, "tag is locked")
    if tag['perm_id']:
        needed_perm = lookup_perm(tag['perm_id'],strict=True)['name']
        if needed_perm not in perms:
            return (False, override, "tag is locked")
    return (True,override,"")

def assert_tag_access(tag_id,user_id=None,force=False):
    access, override, reason = check_tag_access(tag_id,user_id)
    if not access and not (override and force):
        raise koji.ActionNotAllowed, reason

def _tag_build(tag,build,user_id=None,force=False):
    """Tag a build

    This function makes access checks based on user_id, which defaults to the
    user_id of the session.

    Tagging with a locked tag is not allowed unless force is true (and even
    then admin permission is required).

    Retagging is not allowed unless force is true. (retagging changes the order
    of entries will affect which build is the latest)
    """
    tag = get_tag(tag, strict=True)
    build = get_build(build, strict=True)
    tag_id = tag['id']
    build_id = build['id']
    nvr = "%(name)s-%(version)s-%(release)s" % build
    if build['state'] != koji.BUILD_STATES['COMPLETE']:
        # incomplete builds may not be tagged, not even when forced
        state = koji.BUILD_STATES[build['state']]
        raise koji.TagError, "build %s not complete: state %s" % (nvr,state)
    #access check
    assert_tag_access(tag['id'],user_id=user_id,force=force)
    #XXX - add another check based on package ownership?
    # see if it's already tagged
    retag = False
    q = """SELECT build_id FROM tag_listing WHERE tag_id=%(tag_id)i
    AND build_id=%(build_id)i AND active = TRUE FOR UPDATE"""
    #note: tag_listing is unique on (build_id, tag_id, active)
    if _fetchSingle(q,locals()):
        #already tagged
        if not force:
            raise koji.TagError, "build %s already tagged (%s)" % (nvr,tag['name'])
        #otherwise we retag
        retag = True
    event_id = _singleValue("SELECT get_event()")
    if retag:
        #revoke the old tag first
        q = """UPDATE tag_listing SET active=NULL,revoke_event=%(event_id)i
        WHERE tag_id=%(tag_id)i AND build_id=%(build_id)i AND active = TRUE"""
        _dml(q,locals())
    #tag the package
    q = """INSERT INTO tag_listing(tag_id,build_id,active,create_event)
    VALUES(%(tag_id)i,%(build_id)i,TRUE,%(event_id)i)"""
    _dml(q,locals())

def _untag_build(tag,build,user_id=None,strict=True,force=False):
    """Untag a build

    If strict is true, assert that build is actually tagged
    The force option overrides a lock (if the user is an admin)

    This function makes access checks based on user_id, which defaults to the
    user_id of the session.
    """
    tag = get_tag(tag, strict=True)
    build = get_build(build, strict=True)
    tag_id = tag['id']
    build_id = build['id']
    assert_tag_access(tag_id,user_id=user_id,force=force)
    #XXX - add another check based on package ownership?
    q = """UPDATE tag_listing SET active=NULL,revoke_event=get_event()
    WHERE tag_id=%(tag_id)i AND build_id=%(build_id)i AND active = TRUE
    """
    count = _dml(q,locals())
    if count == 0 and strict:
        nvr = "%(name)s-%(version)s-%(release)s" % build
        raise koji.TagError, "build %s not in tag %s" % (nvr,tag['name'])

# tag-group operations
#       add
#       remove
#       block
#       unblock
#       list (readTagGroups)

def grplist_add(taginfo,grpinfo,block=False,force=False,**opts):
    """Add to (or update) group list for tag"""
    #only admins....
    context.session.assertPerm('admin')
    tag = get_tag(taginfo)
    group = lookup_group(grpinfo,create=True)
    block = bool(block)
    # check current group status (incl inheritance)
    groups = get_tag_groups(tag['id'], inherit=True, incl_pkgs=False,incl_reqs=False)
    previous = groups.get(group['id'],None)
    cfg_fields = ('exported','display_name','is_default','uservisible',
                  'description','langonly','biarchonly',)
    if previous is not None:
        #already there (possibly via inheritance)
        if previous['blocked'] and not force:
            raise koji.GenericError, "group %s is blocked in tag %s" % (group['name'],tag['name'])
        #check for duplication and grab old data for defaults
        changed = False
        for field in cfg_fields:
            old = previous[field]
            if opts.has_key(field):
                if opts[field] != old:
                    changed = True
            else:
                opts[field] = old
        if not changed:
            #no point in adding it again with the same data
            return
    #provide available defaults and sanity check data
    opts.setdefault('display_name',group['name'])
    opts.setdefault('biarchonly',False)
    opts.setdefault('exported',True)
    opts.setdefault('uservisible',True)
    # XXX ^^^
    opts['tag_id'] = tag['id']
    opts['grp_id'] = group['id']
    opts['blocked'] = block
    opts['event_id'] = _singleValue("SELECT get_event()")
    #revoke old entry (if present)
    q = """UPDATE group_config SET active=NULL,revoke_event=%(event_id)s
    WHERE active = TRUE AND group_id=%(grp_id)s AND tag_id=%(tag_id)s"""
    _dml(q,opts)
    #add new entry
    x_fields = filter(opts.has_key,cfg_fields)
    params = [ '%%(%s)s' % f for f in x_fields ]
    q = """INSERT INTO group_config(group_id,tag_id,blocked,create_event,%s)
    VALUES (%%(grp_id)s,%%(tag_id)s,%%(blocked)s,%%(event_id)s,%s) """ \
        % ( ','.join(x_fields), ','.join(params))
    _dml(q,opts)

def grplist_remove(taginfo,grpinfo,force=False):
    """Remove group from the list for tag

    Really this shouldn't be used except in special cases
    Most of the time you really want to use the block or unblock functions
    """
    #only admins....
    context.session.assertPerm('admin')
    tag = get_tag(taginfo)
    group = lookup_group(grpinfo, strict=True)
    tag_id = tag['id']
    grp_id = group['id']
    q = """UPDATE group_config SET active=NULL,revoke_event=get_event()
    WHERE active = TRUE AND package_id=%(pkg_id)s AND tag_id=%(tag_id)s"""
    _dml(q,locals())

def grplist_block(taginfo,grpinfo):
    """Block the group in tag"""
    grplist_add(taginfo,grpinfo,block=True)

def grplist_unblock(taginfo,grpinfo):
    """Unblock the group in tag

    If the group is blocked in this tag, then simply remove the block.
    Otherwise, raise an error
    """
    # only admins...
    context.session.assertPerm('admin')
    tag = lookup_tag(taginfo,strict=True)
    group = lookup_group(grpinfo,strict=True)
    tag_id = tag['id']
    grp_id = group['id']
    q = """SELECT blocked FROM group_config
    WHERE active = TRUE AND group_id=%(grp_id)s AND tag_id=%(tag_id)s
    FOR UPDATE"""
    blocked = _singleValue(q,locals())
    if not blocked:
        raise koji.GenericError, "group %s is NOT blocked in tag %s" % (group['name'],tag['name'])
    q = """UPDATE group_config SET active=NULL,revoke_event=get_event()
    WHERE id=%(row_id)s"""
    _dml(q,locals())


# tag-group-pkg operations
#       add
#       remove
#       block
#       unblock
#       list (readTagGroups)

def grp_pkg_add(taginfo,grpinfo,pkg_name,block=False,force=False,**opts):
    """Add package to group for tag"""
    #only admins....
    context.session.assertPerm('admin')
    tag = lookup_tag(taginfo, strict=True)
    group = lookup_group(grpinfo,strict=True)
    block = bool(block)
    # check current group status (incl inheritance)
    groups = get_tag_groups(tag['id'], inherit=True, incl_pkgs=True, incl_reqs=False)
    grp_cfg = groups.get(group['id'],None)
    if grp_cfg is None:
        raise koji.GenericError, "group %s not present in tag %s" % (group['name'],tag['name'])
    elif grp_cfg['blocked']:
        raise koji.GenericError, "group %s is blocked in tag %s" % (group['name'],tag['name'])
    previous = grp_cfg['packagelist'].get(pkg_name,None)
    cfg_fields = ('type','basearchonly','requires')
    if previous is not None:
        #already there (possibly via inheritance)
        if previous['blocked'] and not force:
            raise koji.GenericError, "package %s blocked in group %s, tag %s" \
                    % (pkg_name,group['name'],tag['name'])
        #check for duplication and grab old data for defaults
        changed = False
        for field in cfg_fields:
            old = previous[field]
            if opts.has_key(field):
                if opts[field] != old:
                    changed = True
            else:
                opts[field] = old
        if block:
            #from condition above, either previous is not blocked or force is on,
            #either way, we should add the entry
            changed = True
        if not changed and not force:
            #no point in adding it again with the same data (unless force is on)
            return
    #XXX - sanity check data?
    opts.setdefault('type','default')
    opts['group_id'] = group['id']
    opts['tag_id'] = tag['id']
    opts['package'] = pkg_name
    opts['blocked'] = block
    opts['event_id'] = _singleValue("SELECT get_event()")
    #revoke old entry (if present)
    q = """UPDATE group_package_listing SET active=NULL,revoke_event=%(event_id)s
    WHERE active = TRUE AND group_id=%(group_id)s AND tag_id=%(tag_id)s
        AND package=%(package)s"""
    _dml(q,opts)
    #add new entry
    x_fields = filter(opts.has_key,cfg_fields) \
                + ('group_id','tag_id','package','blocked')
    params = [ '%%(%s)s' % f for f in x_fields ]
    q = """INSERT INTO group_package_listing(create_event,%s)
    VALUES (%%(event_id)s,%s) """ \
        % ( ','.join(x_fields), ','.join(params))
    _dml(q,opts)

def grp_pkg_remove(taginfo,grpinfo,pkg_name,force=False):
    """Remove package from the list for group-tag

    Really this shouldn't be used except in special cases
    Most of the time you really want to use the block or unblock functions
    """
    #only admins....
    context.session.assertPerm('admin')
    tag_id = get_tag_id(taginfo,strict=True)
    grp_id = get_group_id(grpinfo,strict=True)
    q = """UPDATE group_package_listing SET active=NULL,revoke_event=get_event()
    WHERE active = TRUE AND package=%(pkg_name)s AND tag_id=%(tag_id)s
            AND group_id = %(grp_id)s"""
    _dml(q,locals())

def grp_pkg_block(taginfo,grpinfo, pkg_name):
    """Block the package in group-tag"""
    grp_pkg_add(taginfo,grpinfo,pkg_name,block=True)

def grp_pkg_unblock(taginfo,grpinfo,pkg_name):
    """Unblock the package in group-tag

    If blocked (directly) in this tag, then simply remove the block.
    Otherwise, raise an error
    """
    # only admins...
    context.session.assertPerm('admin')
    tag_id = get_tag_id(taginfo,strict=True)
    grp_id = get_group_id(grpinfo,strict=True)
    q = """SELECT blocked FROM group_package_listing
    WHERE active = TRUE AND group_id=%(grp_id)s AND tag_id=%(tag_id)s
            AND package = %(pkg_name)s
    FOR UPDATE"""
    blocked = _singleValue(q, locals(), strict=False)
    if not blocked:
        raise koji.GenericError, "package %s is NOT blocked in group %s, tag %s" \
                    % (pkg_name,grp_id,tag_id)
    q = """UPDATE group_package_listing SET active=NULL,revoke_event=get_event()
    WHERE active = TRUE AND group_id=%(grp_id)s AND tag_id=%(tag_id)s
          AND package = %(pkg_name)s"""
    _dml(q,locals())

# tag-group-req operations
#       add
#       remove
#       block
#       unblock
#       list (readTagGroups)

def grp_req_add(taginfo,grpinfo,reqinfo,block=False,force=False,**opts):
    """Add group requirement to group for tag"""
    #only admins....
    context.session.assertPerm('admin')
    tag = lookup_tag(taginfo, strict=True)
    group = lookup_group(grpinfo, strict=True, create=False)
    req = lookup_group(reqinfo, strict=True, create=False)
    block = bool(block)
    # check current group status (incl inheritance)
    groups = get_tag_groups(tag['id'], inherit=True, incl_pkgs=False, incl_reqs=True)
    grp_cfg = groups.get(group['id'],None)
    if grp_cfg is None:
        raise koji.GenericError, "group %s not present in tag %s" % (group['name'],tag['name'])
    elif grp_cfg['blocked']:
        raise koji.GenericError, "group %s is blocked in tag %s" % (group['name'],tag['name'])
    previous = grp_cfg['grouplist'].get(req['id'],None)
    cfg_fields = ('type','is_metapkg')
    if previous is not None:
        #already there (possibly via inheritance)
        if previous['blocked'] and not force:
            raise koji.GenericError, "requirement on group %s blocked in group %s, tag %s" \
                    % (req['name'],group['name'],tag['name'])
        #check for duplication and grab old data for defaults
        changed = False
        for field in cfg_fields:
            old = previous[field]
            if opts.has_key(field):
                if opts[field] != old:
                    changed = True
            else:
                opts[field] = old
        if block:
            #from condition above, either previous is not blocked or force is on,
            #either way, we should add the entry
            changed = True
        if not changed:
            #no point in adding it again with the same data
            return
    #XXX - sanity check data?
    opts.setdefault('type','mandatory')
    opts['group_id'] = group['id']
    opts['tag_id'] = tag['id']
    opts['req_id'] = req['id']
    opts['blocked'] = block
    opts['event_id'] = _singleValue("SELECT get_event()")
    #revoke old entry (if present)
    q = """UPDATE group_req_listing SET active=NULL,revoke_event=%(event_id)s
    WHERE active = TRUE AND group_id=%(group_id)s AND tag_id=%(tag_id)s
            AND req_id=%(req_id)s"""
    _dml(q,opts)
    #add new entry
    x_fields = filter(opts.has_key,cfg_fields) \
                + ('group_id','tag_id','req_id','blocked')
    params = [ '%%(%s)s' % f for f in x_fields ]
    q = """INSERT INTO group_req_listing(create_event,%s)
    VALUES (%%(event_id)s,%s) """ \
        % ( ','.join(x_fields), ','.join(params))
    _dml(q,opts)

def grp_req_remove(taginfo,grpinfo,reqinfo,force=False):
    """Remove group requirement from the list for group-tag

    Really this shouldn't be used except in special cases
    Most of the time you really want to use the block or unblock functions
    """
    #only admins....
    context.session.assertPerm('admin')
    tag_id = get_tag_id(taginfo,strict=True)
    grp_id = get_group_id(grpinfo,strict=True)
    req_id = get_group_id(reqinfo,strict=True)
    q = """UPDATE group_req_listing SET active=NULL,revoke_event=get_event()
    WHERE active = TRUE AND req_id=%(req_id)s AND tag_id=%(tag_id)s
            AND group_id = %(grp_id)s"""
    _dml(q,locals())

def grp_req_block(taginfo,grpinfo,reqinfo):
    """Block the group requirement in group-tag"""
    grp_req_add(taginfo,grpinfo,reqinfo,block=True)

def grp_req_unblock(taginfo,grpinfo,reqinfo):
    """Unblock the group requirement in group-tag

    If blocked (directly) in this tag, then simply remove the block.
    Otherwise, raise an error
    """
    # only admins...
    context.session.assertPerm('admin')
    tag_id = get_tag_id(taginfo,strict=True)
    grp_id = get_group_id(grpinfo,strict=True)
    req_id = get_group_id(reqinfo,strict=True)
    q = """SELECT blocked FROM group_req_listing
    WHERE active = TRUE AND group_id=%(grp_id)s AND tag_id=%(tag_id)s
            AND req_id = %(req_id)s
    FOR UPDATE"""
    blocked = _singleValue(q,locals())
    if not blocked:
        raise koji.GenericError, "group req %s is NOT blocked in group %s, tag %s" \
                    % (req_id,grp_id,tag_id)
    q = """UPDATE group_req_listing SET active=NULL,revoke_event=get_event()
    WHERE id=%(row_id)s"""
    _dml(q,locals())

def get_tag_groups(tag,event=None,inherit=True,incl_pkgs=True,incl_reqs=True):
    """Return group data for the tag

    If inherit is true, follow inheritance
    If event is specified, query at event
    If incl_pkgs is true (the default), include packagelist data
    If incl_reqs is true (the default), include groupreq data

    Note: the data returned includes some blocked entries that may need to be
    filtered out.
    """
    order = None
    tag = get_tag_id(tag,strict=True)
    taglist = [tag]
    if inherit:
        order = readFullInheritance(tag,event)
        taglist += [link['parent_id'] for link in order]
    evcondition = eventCondition(event)

    # First get the list of groups
    fields = ('name','group_id','tag_id','blocked','exported','display_name',
              'is_default','uservisible','description','langonly','biarchonly',)
    q="""
    SELECT %s FROM group_config JOIN groups ON group_id = id
    WHERE %s AND tag_id = %%(tagid)s
    """ % (",".join(fields),evcondition)
    groups = {}
    for tagid in taglist:
        for group in _multiRow(q,locals(),fields):
            grp_id = group['group_id']
            # we only take the first entry for group as we go through inheritance
            groups.setdefault(grp_id,group)

    if incl_pkgs:
        for group in groups.itervalues():
            group['packagelist'] = {}
        fields = ('group_id','tag_id','package','blocked','type','basearchonly','requires')
        q = """
        SELECT %s FROM group_package_listing
        WHERE %s AND tag_id = %%(tagid)s
        """ % (",".join(fields),evcondition)
        for tagid in taglist:
            for grp_pkg in _multiRow(q,locals(),fields):
                grp_id = grp_pkg['group_id']
                if not groups.has_key(grp_id):
                    #tag does not have this group
                    continue
                group = groups[grp_id]
                if group['blocked']:
                    #ignore blocked groups
                    continue
                pkg_name = grp_pkg['package']
                group['packagelist'].setdefault(pkg_name,grp_pkg)

    if incl_reqs:
        # and now the group reqs
        for group in groups.itervalues():
            group['grouplist'] = {}
        fields = ('group_id','tag_id','req_id','blocked','type','is_metapkg','name')
        q = """SELECT %s FROM group_req_listing JOIN groups on req_id = id
        WHERE %s AND tag_id = %%(tagid)s
        """ % (",".join(fields),evcondition)
        for tagid in taglist:
            for grp_req in _multiRow(q,locals(),fields):
                grp_id = grp_req['group_id']
                if not groups.has_key(grp_id):
                    #tag does not have this group
                    continue
                group = groups[grp_id]
                if group['blocked']:
                    #ignore blocked groups
                    continue
                req_id = grp_req['req_id']
                if not groups.has_key(req_id):
                    #tag does not have this group
                    continue
                elif groups[req_id]['blocked']:
                    #ignore blocked groups
                    continue
                group['grouplist'].setdefault(req_id,grp_req)

    return groups

def readTagGroups(tag,event=None,inherit=True,incl_pkgs=True,incl_reqs=True):
    """Return group data for the tag with blocked entries removed

    Also scrubs data into an xmlrpc-safe format (no integer keys)
    """
    groups = get_tag_groups(tag,event,inherit,incl_pkgs,incl_reqs)
    for group in groups.values():
        #filter blocked entries and collapse to a list
        group['packagelist'] = filter(lambda x: not x['blocked'],
                                      group['packagelist'].values())
        group['grouplist'] = filter(lambda x: not x['blocked'],
                                    group['grouplist'].values())
    #filter blocked entries and collapse to a list
    return filter(lambda x: not x['blocked'],groups.values())

def set_host_enabled(hostname, enabled=True):
    context.session.assertPerm('admin')
    if not get_host(hostname):
        raise koji.GenericError, 'host does not exists: %s' % hostname
    c = context.cnx.cursor()
    c.execute("""UPDATE host SET enabled = %(enabled)s WHERE name = %(hostname)s""", locals())
    context.commit_pending = True

def add_host_to_channel(hostname, channel_name):
    context.session.assertPerm('admin')
    host = get_host(hostname)
    if host == None:
        raise koji.GenericError, 'host does not exists: %s' % hostname
    host_id = host['id']
    channel_id = get_channel_id(channel_name)
    if channel_id == None:
        raise koji.GenericError, 'channel does not exists: %s' % channel_name
    channels = list_channels(host_id)
    for channel in channels:
        if channel['id'] == channel_id:
            raise koji.GenericError, 'host %s is already subscribed to the %s channel' % (hostname, channel_name)
    c = context.cnx.cursor()
    c.execute("""INSERT INTO host_channels (host_id, channel_id) values (%(host_id)d, %(channel_id)d)""", locals())
    context.commit_pending = True

def remove_host_from_channel(hostname, channel_name):
    context.session.assertPerm('admin')
    host = get_host(hostname)
    if host == None:
        raise koji.GenericError, 'host does not exists: %s' % hostname
    host_id = host['id']
    channel_id = get_channel_id(channel_name)
    if channel_id == None:
        raise koji.GenericError, 'channel does not exists: %s' % channel_name
    found = False
    channels = list_channels(host_id)
    for channel in channels:
        if channel['id'] == channel_id:
            found = True
            break
    if not found:
        raise koji.GenericError, 'host %s is not subscribed to the %s channel' % (hostname, channel_name)
    c = context.cnx.cursor()
    c.execute("""DELETE FROM host_channels WHERE host_id = %(host_id)d and channel_id = %(channel_id)d""", locals())
    context.commit_pending = True

def get_ready_hosts():
    """Return information about hosts that are ready to build.

    Hosts set the ready flag themselves
    Note: We ignore hosts that are late checking in (even if a host
        is busy with tasks, it should be checking in quite often).
    """
    c = context.cnx.cursor()
    fields = ('host.id','name','arches','task_load', 'capacity')
    aliases = ('id','name','arches','task_load', 'capacity')
    q = """
    SELECT %s FROM host
        JOIN sessions USING (user_id)
    WHERE enabled = TRUE AND ready = TRUE
        AND expired = FALSE
        AND master IS NULL
        AND update_time > NOW() - '5 minutes'::interval
    """ % ','.join(fields)
    # XXX - magic number in query
    c.execute(q)
    hosts = [dict(zip(aliases,row)) for row in c.fetchall()]
    for host in hosts:
        q = """SELECT channel_id FROM host_channels WHERE host_id=%(id)s"""
        c.execute(q,host)
        host['channels'] = [row[0] for row in c.fetchall()]
    return hosts

def get_all_arches():
    """Return a list of all (canonical) arches available from hosts"""
    ret = {}
    for (arches,) in _fetchMulti('SELECT arches FROM host', {}):
        for arch in arches.split():
            #in a perfect world, this list would only include canonical
            #arches, but not all admins will undertand that.
            ret[koji.canonArch(arch)] = 1
    return ret.keys()

def get_active_tasks():
    """Return data on tasks that are yet to be run"""
    c = context.cnx.cursor()
    fields = ['id','state','channel_id','host_id','arch']
    q = """
    SELECT %s FROM task
    WHERE state IN (%%(FREE)s,%%(ASSIGNED)s)
    ORDER BY priority,create_time
    LIMIT 100
    """ % ','.join(fields)
    c.execute(q,koji.TASK_STATES)
    return [dict(zip(fields,row)) for row in c.fetchall()]

def get_task_descendents(task, childMap=None, request=False):
    if childMap == None:
        childMap = {}
    children = task.getChildren(request=request)
    children.sort(lambda a, b: cmp(a['id'], b['id']))
    # xmlrpclib requires dict keys to be strings
    childMap[str(task.id)] = children
    for child in children:
        get_task_descendents(Task(child['id']), childMap, request)
    return childMap

def repo_init(tag, with_src=False, with_debuginfo=False, event=None):
    """Create a new repo entry in the INIT state, return full repo data

    Returns a dictionary containing
        repo_id, event_id
    """
    logger = logging.getLogger("koji.hub.repo_init")
    state = koji.REPO_INIT
    tinfo = get_tag(tag, strict=True, event=event)
    tag_id = tinfo['id']
    repo_arches = {}
    if tinfo['arches']:
        for arch in tinfo['arches'].split():
            repo_arches[koji.canonArch(arch)] = 1
    repo_id = _singleValue("SELECT nextval('repo_id_seq')")
    if event is None:
        event_id = _singleValue("SELECT get_event()")
    else:
        #make sure event is valid
        q = "SELECT time FROM events WHERE id=%(event)s"
        event_time = _singleValue(q, locals(), strict=True)
        event_id = event
    q = """INSERT INTO repo(id, create_event, tag_id, state)
    VALUES(%(repo_id)s, %(event_id)s, %(tag_id)s, %(state)s)"""
    _dml(q,locals())
    rpms, builds = readTaggedRPMS(tag_id, event=event_id, inherit=True, latest=True)
    groups = readTagGroups(tag_id, event=event_id, inherit=True)
    blocks = [pkg for pkg in readPackageList(tag_id, event=event_id, inherit=True).values() \
                  if pkg['blocked']]
    repodir = koji.pathinfo.repo(repo_id, tinfo['name'])
    os.makedirs(repodir)  #should not already exist
    #index builds
    builds = dict([[build['build_id'],build] for build in builds])
    #index the packages by arch
    packages = {}
    for repoarch in repo_arches:
        packages.setdefault(repoarch, [])
    for rpminfo in rpms:
        if (rpminfo['name'].endswith('-debuginfo') or rpminfo['name'].endswith('-debuginfo-common')) \
                and not with_debuginfo:
            continue
        arch = rpminfo['arch']
        repoarch = koji.canonArch(arch)
        if arch == 'src':
            if not with_src:
                continue
        elif arch == 'noarch':
            pass
        elif repoarch not in repo_arches:
            # Do not create a repo for arches not in the arch list for this tag
            continue
        build = builds[rpminfo['build_id']]
        rpminfo['path'] = "%s/%s" % (koji.pathinfo.build(build), koji.pathinfo.rpm(rpminfo))
        packages.setdefault(repoarch,[]).append(rpminfo)
    #generate comps and groups.spec
    groupsdir = "%s/groups" % (repodir)
    koji.ensuredir(groupsdir)
    comps = koji.generate_comps(groups, expand_groups=True)
    fo = file("%s/comps.xml" % groupsdir,'w')
    fo.write(comps)
    fo.close()

    #link packages
    for arch in packages.iterkeys():
        if arch in ['src','noarch']:
            continue
            # src and noarch special-cased -- see below
        archdir = os.path.join(repodir, arch)
        koji.ensuredir(archdir)
        pkglist = file(os.path.join(repodir, arch, 'pkglist'), 'w')
        logger.info("Creating package list for %s" % arch)
        for rpminfo in packages[arch]:
            pkglist.write(rpminfo['path'].split(os.path.join(koji.pathinfo.topdir, 'packages/'))[1] + '\n')
        #noarch packages
        for rpminfo in packages.get('noarch',[]):
            pkglist.write(rpminfo['path'].split(os.path.join(koji.pathinfo.topdir, 'packages/'))[1] + '\n')
        # srpms
        if with_src:
            srpmdir = "%s/%s" % (repodir,'src')
            koji.ensuredir(srpmdir)
            for rpminfo in packages.get('src',[]):
                pkglist.write(rpminfo['path'].split(os.path.join(koji.pathinfo.topdir, 'packages/'))[1] + '\n')
        pkglist.close()
        #write list of blocked packages
        blocklist = file(os.path.join(repodir, arch, 'blocklist'), 'w')
        logger.info("Creating blocked list for %s" % arch)
        for pkg in blocks:
            blocklist.write(pkg['package_name'])
            blocklist.write('\n')
        blocklist.close()

    # if using an external repo, make sure we've created a directory and pkglist for
    # every arch in the taglist, or any packages of that arch in the external repo
    # won't be processed
    if get_external_repo_list(tinfo['id'], event=event_id):
        for arch in repo_arches:
            pkglist = os.path.join(repodir, arch, 'pkglist')
            if not os.path.exists(pkglist):
                logger.info("Creating missing package list for %s" % arch)
                koji.ensuredir(os.path.dirname(pkglist))
                pkglist_fo = file(pkglist, 'w')
                pkglist_fo.close()
                blocklist = file(os.path.join(repodir, arch, 'blocklist'), 'w')
                logger.info("Creating missing blocked list for %s" % arch)
                for pkg in blocks:
                    blocklist.write(pkg['package_name'])
                    blocklist.write('\n')
                blocklist.close()

    return [repo_id, event_id]

def repo_set_state(repo_id, state, check=True):
    """Set repo state"""
    if check:
        # The repo states are sequential, going backwards makes no sense
        q = """SELECT state FROM repo WHERE id = %(repo_id)s FOR UPDATE"""
        oldstate = _singleValue(q,locals())
        if oldstate > state:
            raise koji.GenericError, "Invalid repo state transition %s->%s" \
                    % (oldstate,state)
    q = """UPDATE repo SET state=%(state)s WHERE id = %(repo_id)s"""
    _dml(q,locals())

def repo_info(repo_id, strict=False):
    fields = (
        ('repo.id', 'id'),
        ('repo.state', 'state'),
        ('repo.create_event', 'create_event'),
        ('events.time','creation_time'),  #for compatibility with getRepo
        ('EXTRACT(EPOCH FROM events.time)','create_ts'),
        ('repo.tag_id', 'tag_id'),
        ('tag.name', 'tag_name'),
    )
    q = """SELECT %s FROM repo
    JOIN tag ON tag_id=tag.id
    JOIN events ON repo.create_event = events.id
    WHERE repo.id = %%(repo_id)s""" % ','.join([f[0] for f in fields])
    return _singleRow(q, locals(), [f[1] for f in fields], strict=strict)

def repo_ready(repo_id):
    """Set repo state to ready"""
    repo_set_state(repo_id,koji.REPO_READY)

def repo_expire(repo_id):
    """Set repo state to expired"""
    repo_set_state(repo_id,koji.REPO_EXPIRED)

def repo_problem(repo_id):
    """Set repo state to problem"""
    repo_set_state(repo_id,koji.REPO_PROBLEM)

def repo_delete(repo_id):
    """Attempt to mark repo deleted, return number of references

    If the number of references is nonzero, no change is made"""
    #get a row lock on the repo
    q = """SELECT state FROM repo WHERE id = %(repo_id)s FOR UPDATE"""
    _singleValue(q,locals())
    references = repo_references(repo_id)
    if not references:
        repo_set_state(repo_id,koji.REPO_DELETED)
    return len(references)

def repo_expire_older(tag_id, event_id):
    """Expire repos for tag older than event"""
    st_ready = koji.REPO_READY
    st_expired = koji.REPO_EXPIRED
    q = """UPDATE repo SET state=%(st_expired)i
    WHERE tag_id = %(tag_id)i
        AND create_event < %(event_id)i
        AND state = %(st_ready)i"""
    _dml(q, locals())

def repo_references(repo_id):
    """Return a list of buildroots that reference the repo"""
    fields = ('id', 'host_id', 'create_event', 'state')
    q = """SELECT %s FROM buildroot WHERE repo_id=%%(repo_id)s
    AND retire_event IS NULL""" % ','.join(fields)
    #check results for bad states
    ret = []
    for data in _multiRow(q, locals(), fields):
        if data['state'] == koji.BR_STATES['EXPIRED']:
            log_error("Error: buildroot %(id)s expired, but has no retire_event" % data)
            continue
        ret.append(data)
    return ret

def get_active_repos():
    """Get data on all active repos

    This is a list of all the repos that the repo daemon needs to worry about.
    """
    fields = (
        ('repo.id', 'id'),
        ('repo.state', 'state'),
        ('repo.create_event', 'create_event'),
        ('EXTRACT(EPOCH FROM events.time)','create_ts'),
        ('repo.tag_id', 'tag_id'),
        ('tag.name', 'tag_name'),
    )
    st_deleted = koji.REPO_DELETED
    q = """SELECT %s FROM repo
    JOIN tag ON tag_id=tag.id
    JOIN events ON repo.create_event = events.id
    WHERE repo.state != %%(st_deleted)s""" % ','.join([f[0] for f in fields])
    return _multiRow(q, locals(), [f[1] for f in fields])

def tag_changed_since_event(event,taglist):
    """Report whether any changes since event affect any of the tags in list

    The function is used by the repo daemon to determine which of its repos
    are up to date.

    This function does not figure inheritance, the calling function should
    expand the taglist to include any desired inheritance.

    Returns: True or False
    """
    c = context.cnx.cursor()
    tables = (
        'tag_listing',
        'tag_inheritance',
        'tag_config',
        'tag_packages',
        'tag_external_repos',
        'group_package_listing',
        'group_req_listing',
        'group_config',
    )
    ret = {}
    for table in tables:
        q = """SELECT tag_id FROM %(table)s
        WHERE create_event > %%(event)s OR revoke_event > %%(event)s
        """ % locals()
        c.execute(q,locals())
        for (tag_id,) in c.fetchall():
            if tag_id in taglist:
                return True
    return False

def create_build_target(name, build_tag, dest_tag):
    """Create a new build target"""

    context.session.assertPerm('admin')

    # Does a target with this name already exist?
    if get_build_targets(info=name):
        raise koji.GenericError("A build target with the name '%s' already exists" % name)

    # Does the build tag exist?
    build_tag_object = get_tag(build_tag)
    if not build_tag_object:
        raise koji.GenericError("build tag '%s' does not exist" % build_tag)
    build_tag = build_tag_object['id']

    # Does the dest tag exist?
    dest_tag_object = get_tag(dest_tag)
    if not dest_tag_object:
        raise koji.GenericError("destination tag '%s' does not exist" % dest_tag)
    dest_tag = dest_tag_object['id']

    #build targets are versioned, so if the target has previously been deleted, it
    #is possible the name is in the system
    id = get_build_target_id(name,create=True)

    insert = """INSERT into build_target_config (build_target_id, build_tag, dest_tag)
    VALUES (%(id)d, %(build_tag)d, %(dest_tag)d)"""

    _dml(insert, locals())

def edit_build_target(buildTargetInfo, name, build_tag, dest_tag):
    """Set the build_tag and dest_tag of an existing build_target to new values"""
    context.session.assertPerm('admin')

    target = lookup_build_target(buildTargetInfo)
    if not target:
        raise koji.GenericError, 'invalid build target: %s' % buildTargetInfo

    buildTargetID = target['id']

    build_tag_object = get_tag(build_tag)
    if not build_tag_object:
        raise koji.GenericError, "build tag '%s' does not exist" % build_tag
    buildTagID = build_tag_object['id']

    dest_tag_object = get_tag(dest_tag)
    if not dest_tag_object:
        raise koji.GenericError, "destination tag '%s' does not exist" % dest_tag
    destTagID = dest_tag_object['id']

    if target['name'] != name:
        # Allow renaming, for parity with tags
        id = _singleValue("""SELECT id from build_target where name = %(name)s""",
                          locals(), strict=False)
        if id is not None:
            raise koji.GenericError, 'name "%s" is already taken by build target %i' % (name, id)

        rename = """UPDATE build_target
        SET name = %(name)s
        WHERE id = %(buildTargetID)i"""

        _dml(rename, locals())

    eventID = _singleValue("SELECT get_event()")

    update = """UPDATE build_target_config
    SET active = NULL,
    revoke_event = %(eventID)i
    WHERE build_target_id = %(buildTargetID)i
    AND active is true
    """

    insert = """INSERT INTO build_target_config
    (build_target_id, build_tag, dest_tag, create_event)
    VALUES
    (%(buildTargetID)i, %(buildTagID)i, %(destTagID)i, %(eventID)i)
    """

    _dml(update, locals())
    _dml(insert, locals())

def delete_build_target(buildTargetInfo):
    """Delete the build target with the given name.  If no build target
    exists, raise a GenericError."""
    context.session.assertPerm('admin')

    target = lookup_build_target(buildTargetInfo)
    if not target:
        raise koji.GenericError, 'invalid build target: %s' % buildTargetInfo

    targetID = target['id']

    #build targets are versioned, so we do not delete them from the db
    #instead we revoke the config entry
    delConfig = """UPDATE build_target_config
    SET active=NULL,revoke_event=get_event()
    WHERE build_target_id = %(targetID)i
    """

    _dml(delConfig, locals())

def get_build_targets(info=None, event=None, buildTagID=None, destTagID=None, queryOpts=None):
    """Return data on all the build targets

    provide event to query at a different time"""
    fields = (
        ('build_target.id', 'id'),
        ('build_tag', 'build_tag'),
        ('dest_tag', 'dest_tag'),
        ('build_target.name', 'name'),
        ('tag1.name', 'build_tag_name'),
        ('tag2.name', 'dest_tag_name'),
    )
    joins = ['build_target ON build_target_config.build_target_id = build_target.id',
             'tag AS tag1 ON build_target_config.build_tag = tag1.id',
             'tag AS tag2 ON build_target_config.dest_tag = tag2.id']
    clauses = [eventCondition(event)]

    if info:
        if isinstance(info, str):
            clauses.append('build_target.name = %(info)s')
        elif isinstance(info, int) or isinstance(info, long):
            clauses.append('build_target.id = %(info)i')
        else:
            raise koji.GenericError, 'invalid type for lookup: %s' % type(info)
    if buildTagID != None:
        clauses.append('build_tag = %(buildTagID)i')
    if destTagID != None:
        clauses.append('dest_tag = %(destTagID)i')

    query = QueryProcessor(columns=[f[0] for f in fields], aliases=[f[1] for f in fields],
                           tables=['build_target_config'], joins=joins, clauses=clauses,
                           values=locals(), opts=queryOpts)
    return query.execute()

def lookup_name(table,info,strict=False,create=False):
    """Find the id and name in the table associated with info.

    Info can be the name to look up, or if create is false it can
    be the id.

    Return value is a dict with keys id and name, or None
    If there is no match, then the behavior depends on the options. If strict,
    then an error is raised. If create, then the required entry is created and
    returned.

    table should be the name of a table with (unique) fields
        id INTEGER
        name TEXT
    Any other fields should have default values, otherwise the
    create option will fail.
    """
    fields = ('id','name')
    if isinstance(info, int) or isinstance(info, long):
        q="""SELECT id,name FROM %s WHERE id=%%(info)d""" % table
    elif isinstance(info, str):
        q="""SELECT id,name FROM %s WHERE name=%%(info)s""" % table
    else:
        raise koji.GenericError, 'invalid type for id lookup: %s' % type(info)
    ret = _singleRow(q,locals(),fields,strict=False)
    if ret is None:
        if strict:
            raise koji.GenericError, 'No such entry in table %s: %s' % (table, info)
        elif create:
            if not isinstance(info, str):
                raise koji.GenericError, 'Name must be a string'
            id = _singleValue("SELECT nextval('%s_id_seq')" % table, strict=True)
            q = """INSERT INTO %s(id,name) VALUES (%%(id)i,%%(info)s)""" % table
            _dml(q,locals())
            return {'id': id, 'name': info}
        else:
            return ret
    return ret

def get_id(table,info,strict=False,create=False):
    """Find the id in the table associated with info."""
    data = lookup_name(table,info,strict,create)
    if data is None:
        return data
    else:
        return data['id']

def get_tag_id(info,strict=False,create=False):
    """Get the id for tag"""
    return get_id('tag',info,strict,create)

def lookup_tag(info,strict=False,create=False):
    """Get the id,name for tag"""
    return lookup_name('tag',info,strict,create)

def get_perm_id(info,strict=False,create=False):
    """Get the id for a permission"""
    return get_id('permissions',info,strict,create)

def lookup_perm(info,strict=False,create=False):
    """Get the id,name for perm"""
    return lookup_name('permissions',info,strict,create)

def get_package_id(info,strict=False,create=False):
    """Get the id for a package"""
    return get_id('package',info,strict,create)

def lookup_package(info,strict=False,create=False):
    """Get the id,name for package"""
    return lookup_name('package',info,strict,create)

def get_channel_id(info,strict=False,create=False):
    """Get the id for a channel"""
    return get_id('channels',info,strict,create)

def lookup_channel(info,strict=False,create=False):
    """Get the id,name for channel"""
    return lookup_name('channels',info,strict,create)

def get_group_id(info,strict=False,create=False):
    """Get the id for a group"""
    return get_id('groups',info,strict,create)

def lookup_group(info,strict=False,create=False):
    """Get the id,name for group"""
    return lookup_name('groups',info,strict,create)

def get_build_target_id(info,strict=False,create=False):
    """Get the id for a build target"""
    return get_id('build_target',info,strict,create)

def lookup_build_target(info,strict=False,create=False):
    """Get the id,name for build target"""
    return lookup_name('build_target',info,strict,create)

def create_tag(name, parent=None, arches=None, perm=None, locked=False):
    """Create a new tag"""

    context.session.assertPerm('admin')

    #see if there is already a tag by this name (active)
    if get_tag(name):
        raise koji.GenericError("A tag with the name '%s' already exists" % name)

    # Does the parent exist?
    if parent:
        parent_tag = get_tag(parent)
        parent_id = parent_tag['id']
        if not parent_tag:
            raise koji.GenericError("Parent tag '%s' could not be found" % parent)
    else:
        parent_id = None

    #there may already be an id for a deleted tag, this will reuse it
    tag_id = get_tag_id(name,create=True)

    c=context.cnx.cursor()

    q = """INSERT INTO tag_config (tag_id,arches,perm_id,locked)
    VALUES (%(tag_id)i,%(arches)s,%(perm)s,%(locked)s)"""
    context.commit_pending = True
    c.execute(q,locals())

    if parent_id:
        data = {'parent_id': parent_id,
                'priority': 0,
                'maxdepth': None,
                'intransitive': False,
                'noconfig': False,
                'pkg_filter': ''}
        writeInheritanceData(get_tag(name)['id'],data)

def get_tag(tagInfo,strict=False,event=None):
    """Get tag information based on the tagInfo.  tagInfo may be either
    a string (the tag name) or an int (the tag ID).
    Returns a map containing the following keys:

    - id
    - name
    - perm_id (may be null)
    - arches (may be null)
    - locked (may be null)

    If there is no tag matching the given tagInfo, and strict is False,
    return None.  If strict is True, raise a GenericError.

    Note that in order for a tag to 'exist', it must have an active entry
    in tag_config. A tag whose name appears in the tag table but has no
    active tag_config entry is considered deleted.
    """
    fields = ('id', 'name', 'perm_id', 'arches', 'locked')
    q = """SELECT %s FROM tag_config
    JOIN tag ON tag_config.tag_id = tag.id
    WHERE %s
        AND  """ % (', '.join(fields), eventCondition(event))
    if isinstance(tagInfo, int):
        q += """tag.id = %(tagInfo)i"""
    elif isinstance(tagInfo, str):
        q += """tag.name = %(tagInfo)s"""
    else:
        raise koji.GenericError, 'invalid type for tagInfo: %s' % type(tagInfo)
    result = _singleRow(q,locals(),fields)
    if not result:
        if strict:
            raise koji.GenericError, "Invalid tagInfo: %r" % tagInfo
        return None
    return result

def edit_tag(tagInfo, **kwargs):
    """Edit information for an existing tag.

    tagInfo specifies the tag to edit
    fields changes are provided as keyword arguments:
        name: rename the tag
        arches: change the arch list
        locked: lock or unlock the tag
        perm: change the permission requirement
    """

    context.session.assertPerm('admin')

    tag = get_tag(tagInfo, strict=True)
    if kwargs.has_key('perm'):
        if kwargs['perm'] is None:
            kwargs['perm_id'] = None
        else:
            kwargs['perm_id'] = get_perm_id(kwargs['perm'],strict=True)

    name = kwargs.get('name')
    if name and tag['name'] != name:
        #attempt to update tag name
        #XXX - I'm not sure we should allow this sort of renaming anyway.
        # while I can see the convenience, it is an untracked change (granted
        # a cosmetic one). The more versioning-friendly way would be to create
        # a new tag with duplicate data and revoke the old tag. This is more
        # of a pain of course :-/  -mikem
        values = {
            'name': name,
            'tagID': tag['id']
            }
        q = """SELECT id FROM tag WHERE name=%(name)s"""
        id = _singleValue(q,values,strict=False)
        if id is not None:
            #new name is taken
            raise koji.GenericError, "Name %s already taken by tag %s" % (name,id)
        update = """UPDATE tag
        SET name = %(name)s
        WHERE id = %(tagID)i"""
        _dml(update, values)

    #check for changes
    data = tag.copy()
    changed = False
    for key in ('perm_id','arches','locked'):
        if kwargs.has_key(key) and data[key] != kwargs[key]:
            changed = True
            data[key] = kwargs[key]
    if not changed:
        return

    #use the same event for both
    data['event_id'] = _singleValue("SELECT get_event()")

    update = """UPDATE tag_config
    SET active = null,
        revoke_event = %(event_id)i
    WHERE tag_id = %(id)i
      AND active is true"""
    _dml(update, data)

    insert = """INSERT INTO tag_config
    (tag_id, arches, perm_id, locked, create_event)
    VALUES
    (%(id)i, %(arches)s, %(perm_id)s, %(locked)s, %(event_id)i)"""
    _dml(insert, data)

def old_edit_tag(tagInfo, name, arches, locked, permissionID):
    """Edit information for an existing tag."""
    return edit_tag(tagInfo, name=name, arches=arches, locked=locked,
                    perm_id=permissionID)


def delete_tag(tagInfo):
    """Delete the specified tag."""

    context.session.assertPerm('admin')

    #We do not ever DELETE tag data. It is versioned -- we revoke it instead.

    def _tagDelete(tableName, value, event, columnName='tag_id'):
        delete = """UPDATE %(tableName)s SET active=NULL,revoke_event=%%(event)i
        WHERE %(columnName)s = %%(value)i AND active = TRUE""" % locals()
        _dml(delete, locals())

    tag = get_tag(tagInfo)
    tagID = tag['id']
    #all these updates are a single transaction, so we use the same event
    eventID = _singleValue("SELECT get_event()")

    _tagDelete('tag_config', tagID, eventID)
    #technically, to 'delete' the tag we only have to revoke the tag_config entry
    #these remaining revocations are more for cleanup.
    _tagDelete('tag_inheritance', tagID, eventID)
    _tagDelete('tag_inheritance', tagID, eventID, 'parent_id')
    _tagDelete('build_target_config', tagID, eventID, 'build_tag')
    _tagDelete('build_target_config', tagID, eventID, 'dest_tag')
    _tagDelete('tag_listing', tagID, eventID)
    _tagDelete('tag_packages', tagID, eventID)
    _tagDelete('tag_external_repos', tagID, eventID)
    _tagDelete('group_config', tagID, eventID)
    _tagDelete('group_req_listing', tagID, eventID)
    _tagDelete('group_package_listing', tagID, eventID)
    # note: we do not delete the entry in the tag table (we can't actually, it
    # is still referenced by the revoked rows).
    # note: there is no need to do anything with the repo entries that reference tagID

def get_external_repo_id(info, strict=False, create=False):
    """Get the id for a build target"""
    return get_id('external_repo', info, strict, create)

def create_external_repo(name, url):
    """Create a new external repo with the given name and url.
    Return a map containing the id, name, and url
    of the new repo."""

    context.session.assertPerm('admin')

    if get_external_repos(info=name):
        raise koji.GenericError, 'An external repo named "%s" already exists' % name

    id = get_external_repo_id(name, create=True)
    if not url.endswith('/'):
        # Ensure the url always ends with /
        url += '/'
    values = {'id': id, 'name': name, 'url': url}
    insert = """INSERT INTO external_repo_config (external_repo_id, url) VALUES (%(id)i, %(url)s)"""
    _dml(insert, values)
    return values

def get_external_repos(info=None, url=None, event=None, queryOpts=None):
    """Get a list of external repos.  If info is not None it may be a
    string (name) or an integer (id).
    If url is not None, filter the list of repos to those matching the
    given url."""
    fields = ['id', 'name', 'url']
    tables = ['external_repo']
    joins = ['external_repo_config ON external_repo_id = id']
    clauses = [eventCondition(event)]
    if info is not None:
        if isinstance(info, str):
            clauses.append('name = %(info)s')
        elif isinstance(info, (int, long)):
            clauses.append('id = %(info)i')
        else:
            raise koji.GenericError, 'invalid type for lookup: %s' % type(info)
    if url:
        clauses.append('url = %(url)s')

    query = QueryProcessor(columns=fields, tables=tables,
                           joins=joins, clauses=clauses,
                           values=locals(), opts=queryOpts)
    return query.execute()

def get_external_repo(info, strict=False, event=None):
    """Get information about a single external repo.
    info can either be a string (name) or an integer (id).
    Returns a map containing the id, name, and url of the
    repo.  If strict is True and no external repo has the
    given name or id, raise an error."""
    repos = get_external_repos(info, event=event)
    if repos:
        return repos[0]
    else:
        if strict:
            raise koji.GenericError, 'invalid repo info: %s' % info
        else:
            return None

def edit_external_repo(info, name=None, url=None):
    """Edit an existing external repo"""

    context.session.assertPerm('admin')

    repo = get_external_repo(info, strict=True)
    repo_id = repo['id']

    if name and name != repo['name']:
        existing_id = _singleValue("""SELECT id FROM external_repo WHERE name = %(name)s""",
                                   locals(), strict=False)
        if existing_id is not None:
            raise koji.GenericError, 'name "%s" is already taken by external repo %i' % (name, existing_id)

        rename = """UPDATE external_repo SET name = %(name)s WHERE id = %(repo_id)i"""
        _dml(rename, locals())

    if url and url != repo['url']:
        if not url.endswith('/'):
            # Ensure the url always ends with /
            url += '/'
        event_id = _singleValue("SELECT get_event()")

        update = """UPDATE external_repo_config
        SET active = NULL, revoke_event = %(event_id)i
        WHERE external_repo_id = %(repo_id)i
        AND active is true"""

        insert = """INSERT INTO external_repo_config
        (external_repo_id, url, create_event)
        VALUES
        (%(repo_id)i, %(url)s, %(event_id)i)"""

        _dml(update, locals())
        _dml(insert, locals())

def delete_external_repo(info):
    """Delete an external repo"""

    context.session.assertPerm('admin')

    repo = get_external_repo(info, strict=True)
    repo_id = repo['id']

    for tag_repo in get_tag_external_repos(repo_info=repo['id']):
        remove_external_repo_from_tag(tag_info=tag_repo['tag_id'],
                                      repo_info=repo_id)

    update = """UPDATE external_repo_config
    SET active = null, revoke_event = get_event()
    WHERE external_repo_id = %(repo_id)i
    AND active = true"""

    _dml(update, locals())

def add_external_repo_to_tag(tag_info, repo_info, priority, event=None):
    """Add an external repo to a tag"""

    context.session.assertPerm('admin')

    tag = get_tag(tag_info, strict=True)
    tag_id = tag['id']
    repo = get_external_repo(repo_info, strict=True)
    repo_id = repo['id']

    tag_repos = get_tag_external_repos(tag_info=tag_id)
    if [tr for tr in tag_repos if tr['external_repo_id'] == repo_id]:
        raise koji.GenericError, 'tag %s already associated with external repo %s' % \
            (tag['name'], repo['name'])
    if [tr for tr in tag_repos if tr['priority'] == priority]:
        raise koji.GenericError, 'tag %s already associated with an external repo at priority %i' % \
            (tag['name'], priority)

    if event is None:
        event_id = _singleValue("SELECT get_event()")
    else:
        event_id = event

    insert = """INSERT INTO tag_external_repos
    (tag_id, external_repo_id, priority, create_event)
    VALUES
    (%(tag_id)i, %(repo_id)i, %(priority)i, %(event_id)i)"""

    _dml(insert, locals())

def remove_external_repo_from_tag(tag_info, repo_info, event=None):
    """Remove an external repo from a tag"""

    context.session.assertPerm('admin')

    tag = get_tag(tag_info, strict=True)
    tag_id = tag['id']
    repo = get_external_repo(repo_info, strict=True)
    repo_id = repo['id']

    if not get_tag_external_repos(tag_info=tag_id, repo_info=repo_id):
        raise koji.GenericError, 'external repo %s not associated with tag %s' % \
            (repo['name'], tag['name'])

    if event is None:
        event_id = _singleValue("SELECT get_event()")
    else:
        event_id = event

    update = """UPDATE tag_external_repos
    SET active = null, revoke_event=%(event_id)i
    WHERE tag_id = %(tag_id)i AND external_repo_id = %(repo_id)i
    AND active = true"""

    _dml(update, locals())

def edit_tag_external_repo(tag_info, repo_info, priority):
    """Edit a tag<->external repo association
    This allows you to update the priority without removing/adding the repo."""

    context.session.assertPerm('admin')

    tag = get_tag(tag_info, strict=True)
    tag_id = tag['id']
    repo = get_external_repo(repo_info, strict=True)
    repo_id = repo['id']

    tag_repos = get_tag_external_repos(tag_info=tag_id, repo_info=repo_id)
    if not tag_repos:
        raise koji.GenericError, 'external repo %s not associated with tag %s' % \
            (repo['name'], tag['name'])
    tag_repo = tag_repos[0]

    if priority != tag_repo['priority']:
        event_id = _singleValue("SELECT get_event()")
        remove_external_repo_from_tag(tag_id, repo_id, event=event_id)
        add_external_repo_to_tag(tag_id, repo_id, priority, event=event_id)

def get_tag_external_repos(tag_info=None, repo_info=None, event=None):
    """
    Get a list of tag<->external repo associations.

    Returns a map containing the following fields:
    tag_id
    tag_name
    external_repo_id
    external_repo_name
    url
    priority
    """
    tables = ['tag_external_repos']
    joins = ['tag ON tag_external_repos.tag_id = tag.id',
             'external_repo ON tag_external_repos.external_repo_id = external_repo.id',
             'external_repo_config ON external_repo.id = external_repo_config.external_repo_id']
    columns = ['tag.id', 'tag.name', 'external_repo.id', 'external_repo.name', 'url', 'priority']
    aliases = ['tag_id', 'tag_name', 'external_repo_id', 'external_repo_name', 'url', 'priority']

    clauses = [eventCondition(event, table='tag_external_repos'), eventCondition(event, table='external_repo_config')]
    if tag_info:
        tag = get_tag(tag_info, strict=True, event=event)
        tag_id = tag['id']
        clauses.append('tag.id = %(tag_id)i')
    if repo_info:
        repo = get_external_repo(repo_info, strict=True, event=event)
        repo_id = repo['id']
        clauses.append('external_repo.id = %(repo_id)i')

    opts = {'order': 'priority'}

    query = QueryProcessor(tables=tables, joins=joins,
                           columns=columns, aliases=aliases,
                           clauses=clauses, values=locals(),
                           opts=opts)
    return query.execute()

def get_external_repo_list(tag_info, event=None):
    """
    Get an ordered list of all external repos associated with the tags in the
    hierarchy rooted at the specified tag.  External repos will be returned
    depth-first, and ordered by priority for each tag.  Duplicates will be
    removed.  Returns a list of maps containing the following fields:

    tag_id
    tag_name
    external_repo_id
    external_repo_name
    url
    priority
    """
    tag = get_tag(tag_info, strict=True, event=event)
    tag_list = [tag['id']]
    for parent in readFullInheritance(tag['id'], event):
        tag_list.append(parent['parent_id'])
    seen_repos = {}
    repos = []
    for tag_id in tag_list:
        for tag_repo in get_tag_external_repos(tag_info=tag_id, event=event):
            if not seen_repos.has_key(tag_repo['external_repo_id']):
                repos.append(tag_repo)
                seen_repos[tag_repo['external_repo_id']] = 1
    return repos

def get_user(userInfo=None,strict=False):
    """Return information about a user.  userInfo may be either a str
    (Kerberos principal) or an int (user id).  A map will be returned with the
    following keys:
      id: user id
      name: user name
      status: user status (int), may be null
      usertype: user type (int), 0 person, 1 for host, may be null
      krb_principal: the user's Kerberos principal"""
    if userInfo is None:
        userInfo = context.session.user_id
        #will still be None if not logged in
    fields = ('id', 'name', 'status', 'usertype', 'krb_principal')
    q = """SELECT %s FROM users WHERE""" % ', '.join(fields)
    if isinstance(userInfo, int) or isinstance(userInfo, long):
        q += """ id = %(userInfo)i"""
    elif isinstance(userInfo, str):
        q += """ (krb_principal = %(userInfo)s or name = %(userInfo)s)"""
    else:
        raise koji.GenericError, 'invalid type for userInfo: %s' % type(userInfo)
    return _singleRow(q,locals(),fields,strict=strict)

def find_build_id(X):
    if isinstance(X,int) or isinstance(X,long):
        return X
    elif isinstance(X,str):
        data = koji.parse_NVR(X)
    elif isinstance(X,dict):
        data = X
    else:
        raise koji.GenericError, "Invalid argument: %r" % X

    if not (data.has_key('name') and data.has_key('version') and
            data.has_key('release')):
        raise koji.GenericError, 'did not provide name, version, and release'

    c=context.cnx.cursor()
    q="""SELECT build.id FROM build JOIN package ON build.pkg_id=package.id
    WHERE package.name=%(name)s AND build.version=%(version)s
    AND build.release=%(release)s
    """
    # contraints should ensure this is unique
    #log_error(koji.db._quoteparams(q,data))
    c.execute(q,data)
    r=c.fetchone()
    #log_error("%r" % r )
    if not r:
        return None
    return r[0]

def get_build(buildInfo, strict=False):
    """Return information about a build.  buildID may be either
    a int ID, a string NVR, or a map containing 'name', 'version'
    and 'release.  A map will be returned containing the following
    keys:
      id: build ID
      package_id: ID of the package built
      package_name: name of the package built
      version
      release
      epoch
      nvr
      state
      task_id: ID of the task that kicked off the build
      owner_id: ID of the user who kicked off the build
      owner_name: name of the user who kicked off the build
      creation_event_id: id of the create_event
      creation_time: time the build was created (text)
      creation_ts: time the build was created (epoch)
      completion_time: time the build was completed (may be null)
      completion_ts: time the build was completed (epoch, may be null)

    If there is no build matching the buildInfo given, and strict is specified,
    raise an error.  Otherwise return None.
    """
    buildID = find_build_id(buildInfo)
    if buildID == None:
        if strict:
            raise koji.GenericError, 'No matching build found: %s' % buildInfo
        else:
            return None

    fields = (('build.id', 'id'), ('build.version', 'version'), ('build.release', 'release'),
              ('build.epoch', 'epoch'), ('build.state', 'state'), ('build.completion_time', 'completion_time'),
              ('build.task_id', 'task_id'), ('events.id', 'creation_event_id'), ('events.time', 'creation_time'),
              ('package.id', 'package_id'), ('package.name', 'package_name'), ('package.name', 'name'),
              ("package.name || '-' || build.version || '-' || build.release", 'nvr'),
              ('EXTRACT(EPOCH FROM events.time)','creation_ts'),
              ('EXTRACT(EPOCH FROM build.completion_time)','completion_ts'),
              ('users.id', 'owner_id'), ('users.name', 'owner_name'))
    query = """SELECT %s
    FROM build
    JOIN events ON build.create_event = events.id
    JOIN package on build.pkg_id = package.id
    JOIN users on build.owner = users.id
    WHERE build.id = %%(buildID)i""" % ', '.join([pair[0] for pair in fields])

    c = context.cnx.cursor()
    c.execute(query, locals())
    result = c.fetchone()

    if not result:
        if strict:
            raise koji.GenericError, 'No matching build found: %s' % buildInfo
        else:
            return None
    else:
        ret = dict(zip([pair[1] for pair in fields], result))
        return ret

def get_rpm(rpminfo, strict=False, multi=False):
    """Get information about the specified RPM

    rpminfo may be any one of the following:
    - a int ID
    - a string N-V-R.A
    - a string N-V-R.A@location
    - a map containing 'name', 'version', 'release', and 'arch'
      (and optionally 'location')

    If specified, location should match the name of an external repo

    A map will be returned, with the following keys:
    - id
    - name
    - version
    - release
    - arch
    - epoch
    - payloadhash
    - size
    - buildtime
    - build_id
    - buildroot_id
    - external_repo_id
    - external_repo_name

    If there is no RPM with the given ID, None is returned, unless strict
    is True in which case an exception is raised

    If more than one RPM matches, and multi is True, then a list of results is
    returned. If multi is False, a single match is returned (an internal one if
    possible).
    """
    fields = (
        ('rpminfo.id', 'id'),
        ('build_id', 'build_id'),
        ('buildroot_id', 'buildroot_id'),
        ('rpminfo.name', 'name'),
        ('version', 'version'),
        ('release', 'release'),
        ('epoch', 'epoch'),
        ('arch', 'arch'),
        ('external_repo_id', 'external_repo_id'),
        ('external_repo.name', 'external_repo_name'),
        ('payloadhash', 'payloadhash'),
        ('size', 'size'),
        ('buildtime', 'buildtime'),
        )
    # we can look up by id or NVRA
    data = None
    if isinstance(rpminfo,(int,long)):
        data = {'id': rpminfo}
    elif isinstance(rpminfo,str):
        data = koji.parse_NVRA(rpminfo)
    elif isinstance(rpminfo,dict):
        data = rpminfo.copy()
    else:
        raise koji.GenericError, "Invalid argument: %r" % rpminfo
    clauses = []
    if data.has_key('id'):
        clauses.append("rpminfo.id=%(id)s")
    else:
        clauses.append("""rpminfo.name=%(name)s AND version=%(version)s
        AND release=%(release)s AND arch=%(arch)s""")
    retry = False
    if data.has_key('location'):
        data['external_repo_id'] = get_external_repo_id(data['location'], strict=True)
        clauses.append("""external_repo_id = %(external_repo_id)i""")
    elif not multi:
        #try to match internal first, otherwise first matching external
        retry = True  #if no internal match
        orig_clauses = list(clauses)  #copy
        clauses.append("""external_repo_id = 0""")

    joins = ['external_repo ON rpminfo.external_repo_id = external_repo.id']

    query = QueryProcessor(columns=[f[0] for f in fields], aliases=[f[1] for f in fields],
                           tables=['rpminfo'], joins=joins, clauses=clauses,
                           values=data)
    if multi:
        return query.execute()
    ret = query.executeOne()
    if ret:
        return ret
    if retry:
        #at this point we have just an NVRA with no internal match. Open it up to externals
        query.clauses = orig_clauses
        ret = query.executeOne()
    if not ret:
        if strict:
            raise koji.GenericError, "No such rpm: %r" % data
        return None
    return ret

def _fetchMulti(query, values):
    """Run the query and return all rows"""
    c = context.cnx.cursor()
    c.execute(query, values)
    results = c.fetchall()
    c.close()
    return results

def _fetchSingle(query, values, strict=False):
    """Run the query and return a single row

    If strict is true, raise an error if the query returns more or less than
    one row."""
    results = _fetchMulti(query, values)
    numRows = len(results)
    if numRows == 0:
        if strict:
            raise koji.GenericError, 'query returned no rows'
        else:
            return None
    elif strict and numRows > 1:
        raise koji.GenericError, 'multiple rows returned for a single row query'
    else:
        return results[0]

def _multiRow(query, values, fields):
    """Return all rows from "query".  Named query parameters
    can be specified using the "values" map.  Results will be returned
    as a list of maps.  Each map in the list will have a key for each
    element in the "fields" list.  If there are no results, an empty
    list will be returned."""
    return [dict(zip(fields, row)) for row in _fetchMulti(query, values)]

def _singleRow(query, values, fields, strict=False):
    """Return a single row from "query".  Named parameters can be
    specified using the "values" map.  The result will be returned as
    as map.  The map will have a key for each element in the "fields"
    list.  If more than one row is returned and "strict" is true, a
    GenericError will be raised.  If no rows are returned, and "strict"
    is True, a GenericError will be raised.  Otherwise None will be
    returned."""
    row = _fetchSingle(query, values, strict)
    if row:
        return dict(zip(fields, row))
    else:
        #strict enforced by _fetchSingle
        return None

def _singleValue(query, values=None, strict=True):
    """Perform a query that returns a single value.

    Note that unless strict is True a return value of None could mean either
    a single NULL value or zero rows returned."""
    if values is None:
        values = {}
    row = _fetchSingle(query, values, strict)
    if row:
        if strict and len(row) > 1:
            raise koji.GenericError, 'multiple fields returned for a single value query'
        return row[0]
    else:
        # don't need to check strict here, since that was already handled by _singleRow()
        return None

def _dml(operation, values):
    """Run an insert, update, or delete. Return number of rows affected"""
    c = context.cnx.cursor()
    c.execute(operation, values)
    ret = c.rowcount
    c.close()
    context.commit_pending = True
    return ret

def get_host(hostInfo, strict=False):
    """Get information about the given host.  hostInfo may be
    either a string (hostname) or int (host id).  A map will be returned
    containign the following data:

    - id
    - user_id
    - name
    - arches
    - task_load
    - capacity
    - ready
    - enabled
    """
    fields = ('id', 'user_id', 'name', 'arches', 'task_load',
              'capacity', 'ready', 'enabled')
    query = """SELECT %s FROM host
    WHERE """ % ', '.join(fields)
    if isinstance(hostInfo, int) or isinstance(hostInfo, long):
        query += """id = %(hostInfo)i"""
    elif isinstance(hostInfo, str):
        query += """name = %(hostInfo)s"""
    else:
        raise koji.GenericError, 'invalid type for hostInfo: %s' % type(hostInfo)

    return _singleRow(query, locals(), fields, strict)

def get_channel(channelInfo, strict=False):
    """Return information about a channel."""
    fields = ('id', 'name')
    query = """SELECT %s FROM channels
    WHERE """ % ', '.join(fields)
    if isinstance(channelInfo, int) or isinstance(channelInfo, long):
        query += """id = %(channelInfo)i"""
    elif isinstance(channelInfo, str):
        query += """name = %(channelInfo)s"""
    else:
        raise koji.GenericError, 'invalid type for channelInfo: %s' % type(channelInfo)

    return _singleRow(query, locals(), fields, strict)


def query_buildroots(hostID=None, tagID=None, state=None, rpmID=None, taskID=None, buildrootID=None, queryOpts=None):
    """Return a list of matching buildroots

    Optional args:
        hostID - only buildroots on host.
        tagID - only buildroots for tag.
        state - only buildroots in state (may be a list)
        rpmID - only the buildroot the specified rpm was built in
        taskID - only buildroots associated with task.
    """
    fields = [('buildroot.id', 'id'), ('buildroot.arch', 'arch'), ('buildroot.state', 'state'),
              ('buildroot.dirtyness', 'dirtyness'), ('buildroot.task_id', 'task_id'),
              ('host.id', 'host_id'), ('host.name', 'host_name'),
              ('repo.id', 'repo_id'), ('repo.state', 'repo_state'),
              ('tag.id', 'tag_id'), ('tag.name', 'tag_name'),
              ('create_events.id', 'create_event_id'), ('create_events.time', 'create_event_time'),
              ('EXTRACT(EPOCH FROM create_events.time)','create_ts'),
              ('retire_events.id', 'retire_event_id'), ('retire_events.time', 'retire_event_time'),
              ('EXTRACT(EPOCH FROM retire_events.time)','retire_ts'),
              ('repo_create.id', 'repo_create_event_id'), ('repo_create.time', 'repo_create_event_time')]

    tables = ['buildroot']
    joins=['host ON host.id = buildroot.host_id',
           'repo ON repo.id = buildroot.repo_id',
           'tag ON tag.id = repo.tag_id',
           'events AS create_events ON create_events.id = buildroot.create_event',
           'LEFT OUTER JOIN events AS retire_events ON buildroot.retire_event = retire_events.id',
           'events AS repo_create ON repo_create.id = repo.create_event']

    clauses = []
    if buildrootID != None:
        if isinstance(buildrootID, list) or isinstance(buildrootID, tuple):
            clauses.append('buildroot.id IN %(buildrootID)s')
        else:
            clauses.append('buildroot.id = %(buildrootID)i')
    if hostID != None:
        clauses.append('host.id = %(hostID)i')
    if tagID != None:
        clauses.append('tag.id = %(tagID)i')
    if state != None:
        if isinstance(state, list) or isinstance(state, tuple):
            clauses.append('buildroot.state IN %(state)s')
        else:
            clauses.append('buildroot.state = %(state)i')
    if rpmID != None:
        joins.insert(0, 'buildroot_listing ON buildroot.id = buildroot_listing.buildroot_id')
        fields.append(('buildroot_listing.is_update', 'is_update'))
        clauses.append('buildroot_listing.rpm_id = %(rpmID)i')
    if taskID != None:
        clauses.append('buildroot.task_id = %(taskID)i')

    query = QueryProcessor(columns=[f[0] for f in fields], aliases=[f[1] for f in fields],
                           tables=tables, joins=joins, clauses=clauses, values=locals(),
                           opts=queryOpts)
    return query.execute()

def get_buildroot(buildrootID, strict=False):
    """Return information about a buildroot.  buildrootID must be an int ID."""

    result = query_buildroots(buildrootID=buildrootID)
    if len(result) == 0:
        if strict:
            raise koji.GenericError, "No such buildroot: %r" % buildrootID
        else:
            return None
    if len(result) > 1:
        #this should be impossible
        raise koji.GenericError, "More that one buildroot with id: %i" % buildrootID
    return result[0]

def list_channels(hostID=None):
    """List channels.  If hostID is specified, only list
    channels associated with the host with that ID."""
    fields = ('id', 'name')
    query = """SELECT %s FROM channels
    """ % ', '.join(fields)
    if hostID != None:
        query += """JOIN host_channels ON channels.id = host_channels.channel_id
        WHERE host_channels.host_id = %(hostID)i"""
    return _multiRow(query, locals(), fields)

def new_package(name,strict=True):
    c = context.cnx.cursor()
    # TODO - table lock?
    # check for existing
    q = """SELECT id FROM package WHERE name=%(name)s"""
    c.execute(q,locals())
    row = c.fetchone()
    if row:
        (pkg_id,) = row
        if strict:
            raise koji.GenericError, "Package already exists [id %d]" % pkg_id
    else:
        q = """SELECT nextval('package_id_seq')"""
        c.execute(q)
        (pkg_id,) = c.fetchone()
        q = """INSERT INTO package (id,name) VALUES (%(pkg_id)s,%(name)s)"""
        context.commit_pending = True
        c.execute(q,locals())
    return pkg_id

def new_build(data):
    """insert a new build entry"""
    data = data.copy()
    if not data.has_key('pkg_id'):
        #see if there's a package name
        name = data.get('name')
        if not name:
            raise koji.GenericError, "No name or package id provided for build"
        data['pkg_id'] = new_package(name,strict=False)
    for f in ('version','release','epoch'):
        if not data.has_key(f):
            raise koji.GenericError, "No %s value for build" % f
    #provide a few default values
    data.setdefault('state',koji.BUILD_STATES['COMPLETE'])
    data.setdefault('completion_time', 'NOW')
    data.setdefault('owner',context.session.user_id)
    data.setdefault('task_id',None)
    #check for existing build
    # TODO - table lock?
    q="""SELECT id,state,task_id FROM build
    WHERE pkg_id=%(pkg_id)d AND version=%(version)s AND release=%(release)s
    FOR UPDATE"""
    row = _fetchSingle(q, data)
    if row:
        id, state, task_id = row
        st_desc = koji.BUILD_STATES[state]
        if st_desc == 'BUILDING':
            # check to see if this is the controlling task
            if data['state'] == state and data.get('task_id','') == task_id:
                #the controlling task must have restarted (and called initBuild again)
                return id
            raise koji.GenericError, "Build already in progress (task %d)" % task_id
            # TODO? - reclaim 'stale' builds (state=BUILDING and task_id inactive)
        if st_desc in ('FAILED','CANCELED'):
            #should be ok to replace
            update = """UPDATE build SET state=%(state)i,task_id=%(task_id)s,
            owner=%(owner)s,completion_time=%(completion_time)s,create_event=get_event()
            WHERE id = %(id)i"""
            data['id'] = id
            _dml(update, data)
            return id
        raise koji.GenericError, "Build already exists (id=%d, state=%s): %r" \
            % (id, st_desc, data)
    #insert the new data
    q="""
    INSERT INTO build (pkg_id,version,release,epoch,state,
            task_id,owner,completion_time)
    VALUES (%(pkg_id)s,%(version)s,%(release)s,%(epoch)s,
            %(state)s,%(task_id)s,%(owner)s,%(completion_time)s)
    """
    _dml(q, data)
    #return build_id
    q="""SELECT currval('build_id_seq')"""
    return _singleValue(q)

def check_noarch_rpms(basepath, rpms):
    """
    If rpms contains any noarch rpms with identical names,
    run rpmdiff against the duplicate rpms.
    Return the list of rpms with any duplicate entries removed (only
    the first entry will be retained).
    """
    result = []
    noarch_rpms = {}
    for relpath in rpms:
        if relpath.endswith('.noarch.rpm'):
            filename = os.path.basename(relpath)
            if noarch_rpms.has_key(filename):
                # duplicate found, add it to the duplicate list
                # but not the result list
                noarch_rpms[filename].append(relpath)
            else:
                noarch_rpms[filename] = [relpath]
                result.append(relpath)
        else:
            result.append(relpath)

    for noarch_list in noarch_rpms.values():
        rpmdiff(basepath, noarch_list)

    return result

def import_build(srpm, rpms, brmap=None, task_id=None, build_id=None, logs=None):
    """Import a build into the database (single transaction)

    Files must be uploaded and specified with path relative to the workdir
    Args:
        srpm - relative path of srpm
        rpms - list of rpms (relative paths)
        brmap - dictionary mapping [s]rpms to buildroot ids
        task_id - associate the build with a task
        build_id - build is a finalization of existing entry
    """
    if brmap is None:
        brmap = {}
    uploadpath = koji.pathinfo.work()
    #verify files exist
    for relpath in [srpm] + rpms:
        fn = "%s/%s" % (uploadpath,relpath)
        if not os.path.exists(fn):
            raise koji.GenericError, "no such file: %s" % fn

    rpms = check_noarch_rpms(uploadpath, rpms)

    #verify buildroot ids from brmap
    found = {}
    for br_id in brmap.values():
        if found.has_key(br_id):
            continue
        found[br_id] = 1
        #this will raise an exception if the buildroot id is invalid
        BuildRoot(br_id)

    #read srpm info
    fn = "%s/%s" % (uploadpath,srpm)
    build = koji.get_header_fields(fn,('name','version','release','epoch',
                                        'sourcepackage'))
    if build['sourcepackage'] != 1:
        raise koji.GenericError, "not a source package: %s" % fn
    build['task_id'] = task_id
    if build_id is None:
        build_id = new_build(build)
    else:
        #build_id was passed in - sanity check
        binfo = get_build(build_id)
        for key in ('name','version','release','epoch','task_id'):
            if build[key] != binfo[key]:
                raise koji.GenericError, "Unable to complete build: %s mismatch (build: %s, rpm: %s)" % (key, binfo[key], build[key])
        if binfo['state'] != koji.BUILD_STATES['BUILDING']:
            raise koji.GenericError, "Unable to complete build: state is %s" \
                    % koji.BUILD_STATES[binfo['state']]
        #update build state
        st_complete = koji.BUILD_STATES['COMPLETE']
        update = """UPDATE build SET state=%(st_complete)i,completion_time=NOW()
        WHERE id=%(build_id)i"""
        _dml(update,locals())
    build['id'] = build_id
    # now to handle the individual rpms
    for relpath in [srpm] + rpms:
        fn = "%s/%s" % (uploadpath,relpath)
        rpminfo = import_rpm(fn,build,brmap.get(relpath))
        import_rpm_file(fn,build,rpminfo)
        add_rpm_sig(rpminfo['id'], koji.rip_rpm_sighdr(fn))
    if logs:
        for key, files in logs.iteritems():
            if not key:
                key = None
            for relpath in files:
                fn = "%s/%s" % (uploadpath,relpath)
                import_build_log(fn, build, subdir=key)
    return build

def import_rpm(fn,buildinfo=None,brootid=None):
    """Import a single rpm into the database

    Designed to be called from import_build.
    """
    if not os.path.exists(fn):
        raise koji.GenericError, "no such file: %s" % fn

    #read rpm info
    hdr = koji.get_rpm_header(fn)
    rpminfo = koji.get_header_fields(hdr,['name','version','release','epoch',
                    'sourcepackage','arch','buildtime','sourcerpm'])
    if rpminfo['sourcepackage'] == 1:
        rpminfo['arch'] = "src"

    #sanity check basename
    basename = os.path.basename(fn)
    expected = "%(name)s-%(version)s-%(release)s.%(arch)s.rpm" % rpminfo
    if basename != expected:
        raise koji.GenericError, "bad filename: %s (expected %s)" % (basename,expected)

    if buildinfo is None:
        #figure it out for ourselves
        if rpminfo['sourcepackage'] == 1:
            buildinfo = rpminfo.copy()
            build_id = find_build_id(buildinfo)
            if build_id:
                # build already exists
                buildinfo['id'] = build_id
            else:
                # create a new build
                buildinfo['id'] = new_build(rpminfo)
        else:
            #figure it out from sourcerpm string
            buildinfo = get_build(koji.parse_NVRA(rpminfo['sourcerpm']))
            if buildinfo is None:
                #XXX - handle case where package is not a source rpm
                #      and we still need to create a new build
                raise koji.GenericError, 'No matching build'
            state = koji.BUILD_STATES[buildinfo['state']]
            if state in ('FAILED', 'CANCELED', 'DELETED'):
                nvr = "%(name)s-%(version)s-%(release)s" % buildinfo
                raise koji.GenericError, "Build is %s: %s" % (state, nvr)
    else:
        srpmname = "%(name)s-%(version)s-%(release)s.src.rpm" % buildinfo
        #either the sourcerpm field should match the build, or the filename
        #itself (for the srpm)
        if rpminfo['sourcepackage'] != 1:
            if rpminfo['sourcerpm'] != srpmname:
                raise koji.GenericError, "srpm mismatch for %s: %s (expected %s)" \
                        % (fn,rpminfo['sourcerpm'],srpmname)
        elif basename != srpmname:
            raise koji.GenericError, "srpm mismatch for %s: %s (expected %s)" \
                    % (fn,basename,srpmname)

    #add rpminfo entry
    rpminfo['id'] = _singleValue("""SELECT nextval('rpminfo_id_seq')""")
    rpminfo['build'] = buildinfo
    rpminfo['build_id'] = buildinfo['id']
    rpminfo['size'] = os.path.getsize(fn)
    rpminfo['payloadhash'] = koji.hex_string(hdr[rpm.RPMTAG_SIGMD5])
    rpminfo['brootid'] = brootid
    q = """INSERT INTO rpminfo (id,name,version,release,epoch,
            build_id,arch,buildtime,buildroot_id,
            external_repo_id,
            size,payloadhash)
    VALUES (%(id)i,%(name)s,%(version)s,%(release)s,%(epoch)s,
            %(build_id)s,%(arch)s,%(buildtime)s,%(brootid)s,
            0,
            %(size)s,%(payloadhash)s)
    """
    _dml(q, rpminfo)

    return rpminfo

def add_external_rpm(rpminfo, external_repo, strict=True):
    """Add an external rpm entry to the rpminfo table

    Differences from import_rpm:
        - entry will have non-zero external_repo_id
        - entry will not reference a build
        - rpm not available to us -- the necessary data is passed in

    The rpminfo arg should contain the following fields:
        - name, version, release, epoch, arch, payloadhash, size, buildtime

    Returns info as get_rpm
    """

    # [!] Calling function should perform access checks

    #sanity check rpminfo
    dtypes = (
        ('name', basestring),
        ('version', basestring),
        ('release', basestring),
        ('epoch', (int, types.NoneType)),
        ('arch', basestring),
        ('payloadhash', str),
        ('size', int),
        ('buildtime', (int, long)))
    for field, allowed in dtypes:
        if not rpminfo.has_key(field):
            raise koji.GenericError, "%s field missing: %r" % (field, rpminfo)
        if not isinstance(rpminfo[field], allowed):
            #this will catch unwanted NULLs
            raise koji.GenericError, "Invalid value for %s: %r" % (field, rpminfo[field])
    #TODO: more sanity checks for payloadhash

    #Check to see if we have it
    data = rpminfo.copy()
    data['location'] = external_repo
    previous = get_rpm(data, strict=False)
    if previous:
        disp = "%(name)s-%(version)s-%(release)s.%(arch)s@%(external_repo_name)s" % previous
        if strict:
            raise koji.GenericError, "external rpm already exists: %s" % disp
        elif data['payloadhash'] != previous['payloadhash']:
            raise koji.GenericError, "hash changed for external rpm: %s (%s -> %s)" \
                    % (disp,  previous['payloadhash'], data['payloadhash'])
        else:
            return previous

    #add rpminfo entry
    rpminfo['external_repo_id'] = get_external_repo_id(external_repo, strict=True)
    rpminfo['id'] = _singleValue("""SELECT nextval('rpminfo_id_seq')""")
    q = """INSERT INTO rpminfo (id, build_id, buildroot_id,
            name, version, release, epoch, arch,
            external_repo_id,
            payloadhash, size, buildtime)
    VALUES (%(id)i, NULL, NULL,
            %(name)s, %(version)s, %(release)s, %(epoch)s, %(arch)s,
            %(external_repo_id)i,
            %(payloadhash)s, %(size)i, %(buildtime)i)
    """
    _dml(q, rpminfo)

    return get_rpm(rpminfo['id'])

def import_build_log(fn, buildinfo, subdir=None):
    """Move a logfile related to a build to the right place"""
    logdir = koji.pathinfo.build_logs(buildinfo)
    if subdir:
        logdir = "%s/%s" % (logdir, subdir)
    koji.ensuredir(logdir)
    final_path = "%s/%s" % (logdir, os.path.basename(fn))
    if os.path.exists(final_path):
        raise koji.GenericError("Error importing build log. %s already exists." % final_path)
    if os.path.islink(fn) or not os.path.isfile(fn):
        raise koji.GenericError("Error importing build log. %s is not a regular file." % fn)
    os.rename(fn,final_path)
    os.symlink(final_path,fn)

def import_rpm_file(fn,buildinfo,rpminfo):
    """Move the rpm file into the proper place

    Generally this is done after the db import
    """
    final_path = "%s/%s" % (koji.pathinfo.build(buildinfo),koji.pathinfo.rpm(rpminfo))
    koji.ensuredir(os.path.dirname(final_path))
    if os.path.exists(final_path):
        raise koji.GenericError("Error importing RPM file. %s already exists." % final_path)
    if os.path.islink(fn) or not os.path.isfile(fn):
        raise koji.GenericError("Error importing RPM file. %s is not a regular file." % fn)
    os.rename(fn,final_path)
    os.symlink(final_path,fn)

def import_build_in_place(build):
    """Import a package already in the packages directory

    This is used for bootstrapping the database
    Parameters:
        build: a dictionary with fields: name, version, release
    """
    # Only an admin may do this
    context.session.assertPerm('admin')
    prev = get_build(build)
    if prev is not None:
        state = koji.BUILD_STATES[prev['state']]
        if state == 'COMPLETE':
            log_error("Skipping build %r, already in db" % build)
            # TODO - check contents against db
            return prev['id']
        elif state not in ('FAILED', 'CANCELED'):
            raise koji.GenericError, "build already exists (%s): %r" % (state, build)
        #otherwise try to reimport
    bdir = koji.pathinfo.build(build)
    srpm = None
    rpms = []
    srpmname = "%(name)s-%(version)s-%(release)s.src.rpm" % build
    # look for srpm first
    srcdir = bdir + "/src"
    if os.path.isdir(srcdir):
        for basename in os.listdir(srcdir):
            if basename != srpmname:
                raise koji.GenericError, "unexpected file: %s" % basename
            srpm = "%s/%s" % (srcdir,basename)
    for arch in os.listdir(bdir):
        if arch == 'src':
            #already done that
            continue
        if arch == "data":
            continue
        adir = "%s/%s" % (bdir,arch)
        if not os.path.isdir(adir):
            raise koji.GenericError, "out of place file: %s" % adir
        for basename in os.listdir(adir):
            fn = "%s/%s" % (adir,basename)
            if not os.path.isfile(fn):
                raise koji.GenericError, "unexpected non-regular file: %s" % fn
            if fn[-4:] != '.rpm':
                raise koji.GenericError, "out of place file: %s" % adir
            #check sourcerpm field
            hdr = koji.get_rpm_header(fn)
            sourcerpm = hdr[rpm.RPMTAG_SOURCERPM]
            if sourcerpm != srpmname:
                raise koji.GenericError, "srpm mismatch for %s: %s (expected %s)" \
                        % (fn,sourcerpm,srpmname)
            rpms.append(fn)
    # actually import
    buildinfo = None
    if srpm is not None:
        rpminfo = import_rpm(srpm)
        add_rpm_sig(rpminfo['id'], koji.rip_rpm_sighdr(srpm))
        buildinfo = rpminfo['build']
        # file already in place
    for fn in rpms:
        rpminfo = import_rpm(fn,buildinfo)
        add_rpm_sig(rpminfo['id'], koji.rip_rpm_sighdr(fn))
    #update build state
    build_id = buildinfo['id']
    st_complete = koji.BUILD_STATES['COMPLETE']
    update = """UPDATE build SET state=%(st_complete)i,completion_time=NOW()
    WHERE id=%(build_id)i"""
    _dml(update,locals())
    return build_id

def add_rpm_sig(an_rpm, sighdr):
    """Store a signature header for an rpm"""
    #calling function should perform permission checks, if applicable
    rinfo = get_rpm(an_rpm, strict=True)
    if rinfo['external_repo_id']:
        raise koji.GenericError, "Not an internal rpm: %s (from %s)" \
                % (an_rpm, rinfo['external_repo_name'])
    binfo = get_build(rinfo['build_id'])
    builddir = koji.pathinfo.build(binfo)
    if not os.path.isdir(builddir):
        raise koji.GenericError, "No such directory: %s" % builddir
    rawhdr = koji.RawHeader(sighdr)
    sigmd5 = koji.hex_string(rawhdr.get(koji.RPM_SIGTAG_MD5))
    if sigmd5 == rinfo['payloadhash']:
        # note: payloadhash is a misnomer, that field is populated with sigmd5.
        sigkey = rawhdr.get(koji.RPM_SIGTAG_GPG)
        if not sigkey:
            sigkey = rawhdr.get(koji.RPM_SIGTAG_PGP)
    else:
        # In older rpms, this field in the signature header does not actually match
        # sigmd5 (I think rpmlib pulls it from SIGTAG_GPG). Anyway, this
        # sanity check fails incorrectly for those rpms, so we fall back to
        # a somewhat more expensive check.
        # ALSO, for these older rpms, the layout of SIGTAG_GPG is different too, so
        # we need to pull that differently as well
        rpm_path = "%s/%s" % (builddir, koji.pathinfo.rpm(rinfo))
        sigmd5, sigkey = _scan_sighdr(sighdr, rpm_path)
        sigmd5 = koji.hex_string(sigmd5)
        if sigmd5 != rinfo['payloadhash']:
            nvra = "%(name)s-%(version)s-%(release)s.%(arch)s" % rinfo
            raise koji.GenericError, "wrong md5 for %s: %s" % (nvra, sigmd5)
    if not sigkey:
        sigkey = ''
        #we use the sigkey='' to represent unsigned in the db (so that uniqueness works)
    else:
        sigkey = koji.get_sigpacket_key_id(sigkey)
    sighash = md5_constructor(sighdr).hexdigest()
    rpm_id = rinfo['id']
    # - db entry
    q = """SELECT sighash FROM rpmsigs WHERE rpm_id=%(rpm_id)i AND sigkey=%(sigkey)s"""
    rows = _fetchMulti(q, locals())
    if rows:
        #TODO[?] - if sighash is the same, handle more gracefully
        nvra = "%(name)s-%(version)s-%(release)s.%(arch)s" % rinfo
        raise koji.GenericError, "Signature already exists for package %s, key %s" % (nvra, sigkey)
    insert = """INSERT INTO rpmsigs(rpm_id, sigkey, sighash)
    VALUES (%(rpm_id)s, %(sigkey)s, %(sighash)s)"""
    _dml(insert, locals())
    # - write to fs
    sigpath = "%s/%s" % (builddir, koji.pathinfo.sighdr(rinfo, sigkey))
    koji.ensuredir(os.path.dirname(sigpath))
    fo = file(sigpath, 'wb')
    fo.write(sighdr)
    fo.close()

def _scan_sighdr(sighdr, fn):
    """Splices sighdr with other headers from fn and queries (no payload)"""
    # This is hackish, but it works
    if not os.path.exists(fn):
        raise koji.GenericError, "No such path: %s" % fn
    if not os.path.isfile(fn):
        raise koji.GenericError, "Not a regular file: %s" % fn
    #XXX should probably add an option to splice_rpm_sighdr to handle this instead
    sig_start, sigsize = koji.find_rpm_sighdr(fn)
    hdr_start = sig_start + sigsize
    hdrsize = koji.rpm_hdr_size(fn, hdr_start)
    inp = file(fn, 'rb')
    outp = tempfile.TemporaryFile(mode='w+b')
    #before signature
    outp.write(inp.read(sig_start))
    #signature
    outp.write(sighdr)
    inp.seek(sigsize, 1)
    #main header
    outp.write(inp.read(hdrsize))
    inp.close()
    outp.seek(0,0)
    ts = rpm.TransactionSet()
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES|rpm._RPMVSF_NODIGESTS)
    #(we have no payload, so verifies would fail otherwise)
    hdr = ts.hdrFromFdno(outp.fileno())
    outp.close()
    sig = hdr[rpm.RPMTAG_SIGGPG]
    if not sig:
        sig = hdr[rpm.RPMTAG_SIGPGP]
    return hdr[rpm.RPMTAG_SIGMD5], sig

def check_rpm_sig(an_rpm, sigkey, sighdr):
    #verify that the provided signature header matches the key and rpm
    rinfo = get_rpm(an_rpm, strict=True)
    binfo = get_build(rinfo['build_id'])
    builddir = koji.pathinfo.build(binfo)
    rpm_path = "%s/%s" % (builddir, koji.pathinfo.rpm(rinfo))
    if not os.path.exists(rpm_path):
        raise koji.GenericError, "No such path: %s" % rpm_path
    if not os.path.isfile(rpm_path):
        raise koji.GenericError, "Not a regular file: %s" % rpm_path
    fd, temp = tempfile.mkstemp()
    os.close(fd)
    try:
        koji.splice_rpm_sighdr(sighdr, rpm_path, temp)
        ts = rpm.TransactionSet()
        ts.setVSFlags(0)  #full verify
        fo = file(temp, 'rb')
        hdr = ts.hdrFromFdno(fo.fileno())
        fo.close()
    except:
        try:
            os.unlink(temp)
        except:
            pass
        raise
    raw_key = hdr[rpm.RPMTAG_SIGGPG]
    if not raw_key:
        raw_key = hdr[rpm.RPMTAG_SIGPGP]
    if not raw_key:
        found_key = None
    else:
        found_key = koji.get_sigpacket_key_id(raw_key)
    if sigkey != found_key:
        raise koji.GenericError, "Signature key mismatch: got %s, expected %s" \
                              % (found_key, sigkey)
    os.unlink(temp)



def query_rpm_sigs(rpm_id=None, sigkey=None, queryOpts=None):
    fields = ('rpm_id', 'sigkey', 'sighash')
    clauses = []
    if rpm_id is not None:
        clauses.append("rpm_id=%(rpm_id)s")
    if sigkey is not None:
        clauses.append("sigkey=%(sigkey)s")
    query = QueryProcessor(columns=fields, tables=('rpmsigs',), clauses=clauses,
                           values=locals(), opts=queryOpts)
    return query.execute()

def write_signed_rpm(an_rpm, sigkey, force=False):
    """Write a signed copy of the rpm"""
    context.session.assertPerm('sign')
    #XXX - still not sure if this is the right restriction
    rinfo = get_rpm(an_rpm, strict=True)
    if rinfo['external_repo_id']:
        raise koji.GenericError, "Not an internal rpm: %s (from %s)" \
                % (an_rpm, rinfo['external_repo_name'])
    binfo = get_build(rinfo['build_id'])
    nvra = "%(name)s-%(version)s-%(release)s.%(arch)s" % rinfo
    builddir = koji.pathinfo.build(binfo)
    rpm_path = "%s/%s" % (builddir, koji.pathinfo.rpm(rinfo))
    if not os.path.exists(rpm_path):
        raise koji.GenericError, "No such path: %s" % rpm_path
    if not os.path.isfile(rpm_path):
        raise koji.GenericError, "Not a regular file: %s" % rpm_path
    #make sure we have it in the db
    rpm_id = rinfo['id']
    q = """SELECT sighash FROM rpmsigs WHERE rpm_id=%(rpm_id)i AND sigkey=%(sigkey)s"""
    row = _fetchSingle(q, locals())
    if not row:
        raise koji.GenericError, "No cached signature for package %s, key %s" % (nvra, sigkey)
    (sighash,) = row
    signedpath = "%s/%s" % (builddir, koji.pathinfo.signed(rinfo, sigkey))
    if os.path.exists(signedpath):
        if not force:
            #already present
            return
        else:
            os.unlink(signedpath)
    sigpath = "%s/%s" % (builddir, koji.pathinfo.sighdr(rinfo, sigkey))
    fo = file(sigpath, 'rb')
    sighdr = fo.read()
    fo.close()
    koji.ensuredir(os.path.dirname(signedpath))
    koji.splice_rpm_sighdr(sighdr, rpm_path, signedpath)


def tag_history(build=None, tag=None, package=None, queryOpts=None):
    """Returns historical tag data

    package: only for given package
    build: only for given build
    tag: only for given tag
    """
    fields = ('build.id', 'package.name', 'build.version', 'build.release',
              'tag.id', 'tag.name', 'tag_listing.active',
              'tag_listing.create_event', 'tag_listing.revoke_event',
              'EXTRACT(EPOCH FROM ev1.time)', 'EXTRACT(EPOCH FROM ev2.time)',)
    aliases = ('build_id', 'name', 'version', 'release',
              'tag_id', 'tag_name', 'active',
              'create_event', 'revoke_event',
              'create_ts', 'revoke_ts',)
    st_complete = koji.BUILD_STATES['COMPLETE']
    tables = ['tag_listing']
    joins = ["tag ON tag.id = tag_listing.tag_id",
             "build ON build.id = tag_listing.build_id",
             "package ON package.id = build.pkg_id",
             "events AS ev1 ON ev1.id = tag_listing.create_event",
             "LEFT OUTER JOIN events AS ev2 ON ev2.id = tag_listing.revoke_event", ]
    clauses = []
    if tag is not None:
        tag_id = get_tag_id(tag, strict=True)
        clauses.append("tag.id = %(tag_id)i")
    if build is not None:
        build_id = get_build(build, strict=True)['id']
        clauses.append("build.id = %(build_id)i")
    if package is not None:
        pkg_id = get_package_id(package, strict=True)
        clauses.append("package.id = %(pkg_id)i")
    query = QueryProcessor(columns=fields, aliases=aliases, tables=tables,
                           joins=joins, clauses=clauses, values=locals(),
                           opts=queryOpts)
    return query.execute()

def untagged_builds(name=None, queryOpts=None):
    """Returns the list of untagged builds"""
    fields = ('build.id', 'package.name', 'build.version', 'build.release')
    aliases = ('id', 'name', 'version', 'release')
    st_complete = koji.BUILD_STATES['COMPLETE']
    tables = ('build',)
    joins = []
    if name is None:
        joins.append("""package ON package.id = build.pkg_id""")
    else:
        joins.append("""package ON package.name=%(name)s AND package.id = build.pkg_id""")
    joins.append("""LEFT OUTER JOIN tag_listing ON tag_listing.build_id = build.id
                    AND tag_listing.active = TRUE""")
    clauses = ["tag_listing.tag_id IS NULL", "build.state = %(st_complete)i"]
    #q = """SELECT build.id, package.name, build.version, build.release
    #FROM build
    #    JOIN package on package.id = build.pkg_id
    #    LEFT OUTER JOIN tag_listing ON tag_listing.build_id = build.id
    #        AND tag_listing.active IS TRUE
    #WHERE tag_listing.tag_id IS NULL AND build.state = %(st_complete)i"""
    #return _multiRow(q, locals(), aliases)
    query = QueryProcessor(columns=fields, aliases=aliases, tables=tables,
                           joins=joins, clauses=clauses, values=locals(),
                           opts=queryOpts)
    return query.execute()

def build_map():
    """Map which builds were used in the buildroots of other builds

    To be used for garbage collection
    """
    # find rpms whose buildroots we were in
    st_complete = koji.BUILD_STATES['COMPLETE']
    fields = ('used', 'built')
    q = """SELECT DISTINCT used.id, built.id
    FROM buildroot_listing
        JOIN rpminfo AS r_used ON r_used.id = buildroot_listing.rpm_id
        JOIN rpminfo AS r_built ON r_built.buildroot_id = buildroot_listing.buildroot_id
        JOIN build AS used ON used.id = r_used.build_id
        JOIN build AS built ON built.id = r_built.build_id
    WHERE built.state = %(st_complete)i AND used.state =%(st_complete)i"""
    return _multiRow(q, locals(), fields)

def build_references(build_id, limit=None):
    """Returns references to a build

    This call is used to determine whether a build can be deleted
    The optional limit arg is used to limit the size of the buildroot
    references.
    """
    #references (that matter):
    #   tag_listing
    #   buildroot_listing (via rpminfo)
    #   ?? rpmsigs (via rpminfo)
    ret = {}

    # find tags
    q = """SELECT tag_id, tag.name FROM tag_listing JOIN tag on tag_id = tag.id
    WHERE build_id = %(build_id)i AND active = TRUE"""
    ret['tags'] = _multiRow(q, locals(), ('id', 'name'))

    #we'll need the component rpm ids for the rest
    q = """SELECT id FROM rpminfo WHERE build_id=%(build_id)i"""
    rpm_ids = _fetchMulti(q, locals())

    # find rpms whose buildroots we were in
    st_complete = koji.BUILD_STATES['COMPLETE']
    fields = ('id', 'name', 'version', 'release', 'arch', 'build_id')
    idx = {}
    q = """SELECT rpminfo.id, rpminfo.name, rpminfo.version, rpminfo.release, rpminfo.arch, rpminfo.build_id
    FROM buildroot_listing
        JOIN rpminfo ON rpminfo.buildroot_id = buildroot_listing.buildroot_id
        JOIN build on rpminfo.build_id = build.id
    WHERE buildroot_listing.rpm_id = %(rpm_id)s
        AND build.state = %(st_complete)i"""
    if limit is not None:
        q += "\nLIMIT %(limit)i"
    for (rpm_id,) in rpm_ids:
        for row in _multiRow(q, locals(), fields):
            idx.setdefault(row['id'], row)
        if limit is not None and len(idx) > limit:
            break
    ret['rpms'] = idx.values()

    # find timestamp of most recent use in a buildroot
    q = """SELECT buildroot.create_event
    FROM buildroot_listing
        JOIN buildroot ON buildroot_listing.buildroot_id = buildroot.id
    WHERE buildroot_listing.rpm_id = %(rpm_id)s
    ORDER BY buildroot.create_event DESC
    LIMIT 1"""
    event_id = -1
    for (rpm_id,) in rpm_ids:
        tmp_id = _singleValue(q, locals(), strict=False)
        if tmp_id is not None and tmp_id > event_id:
            event_id = tmp_id
    if event_id == -1:
        ret['last_used'] = None
    else:
        q = """SELECT EXTRACT(EPOCH FROM get_event_time(%(event_id)i))"""
        ret['last_used'] = _singleValue(q, locals())
    return ret

def delete_build(build, strict=True, min_ref_age=604800):
    """delete a build, if possible

    Attempts to delete a build. A build can only be deleted if it is
    unreferenced.

    If strict is true (default), an exception is raised if the build cannot
    be deleted.

    Note that a deleted build is not completely gone. It is marked deleted and some
    data remains in the database.  Mainly, the rpms are removed.

    Note in particular that deleting a build DOES NOT free any NVRs (or NVRAs) for
    reuse.

    Returns True if successful, False otherwise
    """
    context.session.assertPerm('admin')
    binfo = get_build(build, strict=True)
    refs = build_references(binfo['id'], limit=10)
    if refs['tags']:
        if strict:
            raise koji.GenericError, "Cannot delete build, tagged: %s" % refs['tags']
        return False
    if refs['rpms']:
        if strict:
            raise koji.GenericError, "Cannot delete build, used in buildroots: %s" % refs['rpms']
        return False
    if refs['last_used']:
        age = time.time() - refs['last_used']
        if age < min_ref_age:
            if strict:
                raise koji.GenericError, "Cannot delete build, used in recent buildroot"
            return False
    #otherwise we can delete it
    _delete_build(binfo)
    return True

def _delete_build(binfo):
    """Delete a build (no reference checks)

    Please consider calling delete_build instead
    """
    # build-related data:
    #   build   KEEP (marked deleted)
    #   task ??
    #   tag_listing REVOKE (versioned) (but should ideally be empty anyway)
    #   rpminfo KEEP
    #           buildroot_listing KEEP (but should ideally be empty anyway)
    #           rpmsigs DELETE
    #   files on disk: DELETE
    build_id = binfo['id']
    q = """SELECT id FROM rpminfo WHERE build_id=%(build_id)i"""
    rpm_ids = _fetchMulti(q, locals())
    for (rpm_id,) in rpm_ids:
        delete = """DELETE FROM rpmsigs WHERE rpm_id=%(rpm_id)i"""
        _dml(delete, locals())
    event_id = _singleValue("SELECT get_event()")
    update = """UPDATE tag_listing SET revoke_event=%(event_id)i, active=NULL
    WHERE active = TRUE AND build_id=%(build_id)i"""
    _dml(update, locals())
    st_deleted = koji.BUILD_STATES['DELETED']
    update = """UPDATE build SET state=%(st_deleted)i WHERE id=%(build_id)i"""
    _dml(update, locals())
    #now clear the build dir
    builddir = koji.pathinfo.build(binfo)
    rv = os.system(r"find '%s' -xdev \! -type d -print0 |xargs -0 rm -f" % builddir)
    if rv != 0:
        raise koji.GenericError, 'file removal failed (code %r) for %s' % (rv, builddir)
    #and clear out the emptied dirs
    os.system(r"find '%s' -xdev -depth -type d -print0 |xargs -0 rmdir" % builddir)

def reset_build(build):
    """Reset a build so that it can be reimported

    WARNING: this function is potentially destructive. use with care.
    nulls task_id
    sets state to CANCELED
    clears data in rpminfo
    removes rpminfo entries from any buildroot_listings [!]
    remove files related to the build

    note, we don't actually delete the build data, so tags
    remain intact
    """
    # Only an admin may do this
    context.session.assertPerm('admin')
    binfo = get_build(build)
    if not binfo:
        #nothing to do
        return
    q = """SELECT id FROM rpminfo WHERE build_id=%(id)i"""
    ids = _fetchMulti(q, binfo)
    for (rpm_id,) in ids:
        delete = """DELETE FROM rpmsigs WHERE rpm_id=%(rpm_id)i"""
        _dml(delete, locals())
        delete = """DELETE FROM buildroot_listing WHERE rpm_id=%(rpm_id)i"""
        _dml(delete, locals())
    delete = """DELETE FROM rpminfo WHERE build_id=%(id)i"""
    _dml(delete, binfo)
    binfo['state'] = koji.BUILD_STATES['CANCELED']
    update = """UPDATE build SET state=%(state)i, task_id=NULL WHERE id=%(id)i"""
    _dml(update, binfo)
    #now clear the build dir
    builddir = koji.pathinfo.build(binfo)
    rv = os.system("find '%s' -xdev \\! -type d -print0 |xargs -0 rm -f" % builddir)
    if rv != 0:
        raise koji.GenericError, 'file removal failed (code %r) for %s' % (rv, builddir)

def cancel_build(build_id, cancel_task=True):
    """Cancel a build

    Calling function should perform permission checks.

    If the build is associated with a task, cancel the task as well (unless
    cancel_task is False).
    Return True if the build was successfully canceled, False if not.

    The cancel_task option is used to prevent loops between task- and build-
    cancellation.
    """
    st_canceled = koji.BUILD_STATES['CANCELED']
    st_building = koji.BUILD_STATES['BUILDING']
    update = """UPDATE build
    SET state = %(st_canceled)i, completion_time = NOW()
    WHERE id = %(build_id)i AND state = %(st_building)i"""
    _dml(update, locals())
    build = get_build(build_id)
    if build['state'] != st_canceled:
        return False
    task_id = build['task_id']
    if task_id != None:
        build_notification(task_id, build_id)
        if cancel_task:
            Task(task_id).cancelFull(strict=False)
    return True

def _get_build_target(task_id):
    # XXX Should we be storing a reference to the build target
    # in the build table for reproducibility?
    task = Task(task_id)
    request = task.getRequest()
    # request is (path-to-srpm, build-target-name, map-of-other-options)
    if request[1]:
        ret = get_build_targets(request[1])
        return ret[0]
    else:
        return None

def get_notification_recipients(build, tag_id, state):
    """
    Return the list of email addresses that should be notified about events
    involving the given build and tag.  This could be the build into that tag
    succeeding or failing, or the build being manually tagged or untagged from
    that tag.

    The list will contain email addresss for all users who have registered for
    notifications on the package or tag (or both), as well as the package owner
    for this tag and the user who submitted the build.  The list will not contain
    duplicates.
    """
    
    clauses = []

    if build:
        package_id = build['package_id']
        clauses.append('package_id = %(package_id)i OR package_id IS NULL')
    else:
        clauses.append('package_id IS NULL')
    if tag_id:
        clauses.append('tag_id = %(tag_id)i OR tag_id IS NULL')
    else:
        clauses.append('tag_id IS NULL')
    if state != koji.BUILD_STATES['COMPLETE']:
        clauses.append('success_only = FALSE')

    query = QueryProcessor(columns=('email',), tables=['build_notifications'],
                           clauses=clauses, values=locals(),
                           opts={'asList':True})
    emails = [result[0] for result in query.execute()]

    email_domain = context.opts['EmailDomain']
    notify_on_success = context.opts['NotifyOnSuccess']

    if notify_on_success is True or state != koji.BUILD_STATES['COMPLETE']:
        # user who submitted the build
        emails.append('%s@%s' % (build['owner_name'], email_domain))

        if tag_id:
            packages = readPackageList(pkgID=package_id, tagID=tag_id, inherit=True)
            # owner of the package in this tag, following inheritance
            emails.append('%s@%s' % (packages[package_id]['owner_name'], email_domain))
        #FIXME - if tag_id is None, we don't have a good way to get the package owner.
        #   using all package owners from all tags would be way overkill.

    emails_uniq = dict(zip(emails, [1] * len(emails))).keys()
    return emails_uniq

def tag_notification(is_successful, tag_id, from_id, build_id, user_id, ignore_success=False, failure_msg=''):
    if context.opts.get('DisableNotifications'):
        return
    if is_successful:
        state = koji.BUILD_STATES['COMPLETE']
    else:
        state = koji.BUILD_STATES['FAILED']
    recipients = {}
    build = get_build(build_id)
    if not build:
        # the build doesn't exist, so there's nothing to send a notification about
        return None
    if tag_id:
        tag = get_tag(tag_id)
        for email in get_notification_recipients(build, tag['id'], state):
            recipients[email] = 1
    if from_id:
        from_tag = get_tag(from_id)
        for email in get_notification_recipients(build, from_tag['id'], state):
            recipients[email] = 1
    recipients_uniq = recipients.keys()
    if len(recipients_uniq) > 0 and not (is_successful and ignore_success):
        task_id = make_task('tagNotification', [recipients_uniq, is_successful, tag_id, from_id, build_id, user_id, ignore_success, failure_msg])
        return task_id
    return None

def build_notification(task_id, build_id):
    if context.opts.get('DisableNotifications'):
        return
    build = get_build(build_id)
    target = _get_build_target(task_id)

    dest_tag = None
    if target:
        dest_tag = target['dest_tag']

    if build['state'] == koji.BUILD_STATES['BUILDING']:
        raise koji.GenericError, 'never send notifications for incomplete builds'

    web_url = context.opts.get('KojiWebURL', 'http://localhost/koji')

    recipients = get_notification_recipients(build, dest_tag, build['state'])
    if len(recipients) > 0:
        make_task('buildNotification', [recipients, build, target, web_url])

def get_build_notifications(user_id):
    fields = ('id', 'user_id', 'package_id', 'tag_id', 'success_only', 'email')
    query = """SELECT %s
    FROM build_notifications
    WHERE user_id = %%(user_id)i
    """ % ', '.join(fields)
    return _multiRow(query, locals(), fields)

def new_group(name):
    """Add a user group to the database"""
    context.session.assertPerm('admin')
    if get_user(name):
        raise koji.GenericError, 'user/group already exists: %s' % name
    return context.session.createUser(name, usertype=koji.USERTYPES['GROUP'])

def add_group_member(group,user):
    """Add user to group"""
    context.session.assertPerm('admin')
    group = get_user(group)
    user = get_user(user)
    if group['usertype'] != koji.USERTYPES['GROUP']:
        raise koji.GenericError, "Not a group: %(name)s" % group
    if user['usertype'] == koji.USERTYPES['GROUP']:
        raise koji.GenericError, "Groups cannot be members of other groups"
    #check to see if user is already a member
    user_id = user['id']
    group_id = group['id']
    q = """SELECT user_id FROM user_groups
    WHERE active = TRUE AND user_id = %(user_id)i
        AND group_id = %(group_id)s
    FOR UPDATE"""
    row = _fetchSingle(q, locals(), strict=False)
    if row:
        raise koji.GenericError, "User already in group"
    insert = """INSERT INTO user_groups (user_id,group_id)
    VALUES(%(user_id)i,%(group_id)i)"""
    _dml(insert,locals())


def drop_group_member(group,user):
    """Drop user from group"""
    context.session.assertPerm('admin')
    group = get_user(group)
    user = get_user(user)
    if group['usertype'] != koji.USERTYPES['GROUP']:
        raise koji.GenericError, "Not a group: %(name)s" % group
    user_id = user['id']
    group_id = group['id']
    insert = """UPDATE user_groups
    SET active=NULL, revoke_event=get_event()
    WHERE active = TRUE AND user_id = %(user_id)i
        AND group_id = %(group_id)i"""
    _dml(insert,locals())

def get_group_members(group):
    """Get the members of a group"""
    context.session.assertPerm('admin')
    group = get_user(group)
    if group['usertype'] != koji.USERTYPES['GROUP']:
        raise koji.GenericError, "Not a group: %(name)s" % group
    group_id = group['id']
    fields = ('id','name','usertype','krb_principal')
    q = """SELECT %s FROM user_groups
    JOIN users ON user_id = users.id
    WHERE active = TRUE AND group_id = %%(group_id)i""" % ','.join(fields)
    return _multiRow(q, locals(), fields)

def set_user_status(user, status):
    context.session.assertPerm('admin')
    if not koji.USER_STATUS.get(status):
        raise koji.GenericError, 'invalid status: %s' % status
    if user['status'] == status:
        # nothing to do
        return
    update = """UPDATE users SET status = %(status)i WHERE id = %(user_id)i"""
    user_id = user['id']
    rows = _dml(update, locals())
    # sanity check
    if rows == 0:
        raise koji.GenericError, 'invalid user ID: %i' % user_id

class QueryProcessor(object):
    """
    Build a query from its components.
    - columns, aliases, tables: lists of the column names to retrieve,
      the tables to retrieve them from, and the key names to use when
      returning values as a map, respectively
    - joins: a list of joins in the form 'table1 ON table1.col1 = table2.col2', 'JOIN' will be
             prepended automatically; if extended join syntax (LEFT, OUTER, etc.) is required,
             it can be specified, and 'JOIN' will not be prepended
    - clauses: a list of where clauses in the form 'table1.col1 OPER table2.col2-or-variable';
               each clause will be surrounded by parentheses and all will be AND'ed together
    - values: the map that will be used to replace any substitution expressions in the query
    - opts: a map of query options; currently supported options are:
        countOnly: if True, return an integer indicating how many results would have been
                   returned, rather than the actual query results
        order: a column or alias name to use in the 'ORDER BY' clause
        offset: an integer to use in the 'OFFSET' clause
        limit: an integer to use in the 'LIMIT' clause
        asList: if True, return results as a list of lists, where each list contains the
                column values in query order, rather than the usual list of maps
    """
    def __init__(self, columns=None, aliases=None, tables=None,
                 joins=None, clauses=None, values=None, opts=None):
        self.columns = columns
        self.aliases = aliases
        if columns and aliases:
            if len(columns) != len(aliases):
                raise StandardError, 'column and alias lists must be the same length'
            self.colsByAlias = dict(zip(aliases, columns))
        else:
            self.colsByAlias = {}
        self.tables = tables
        self.joins = joins
        self.clauses = clauses
        if values:
            self.values = values
        else:
            self.values = {}
        if opts:
            self.opts = opts
        else:
            self.opts = {}

    def countOnly(self, count):
        self.opts['countOnly'] = count

    def __str__(self):
        query = \
"""
SELECT %(col_str)s
  FROM %(table_str)s
%(join_str)s
%(clause_str)s
 %(order_str)s
%(offset_str)s
 %(limit_str)s
"""
        if self.opts.get('countOnly'):
            if self.opts.get('offset') or self.opts.get('limit'):
                # If we're counting with an offset and/or limit, we need
                # to wrap the offset/limited query and then count the results,
                # rather than trying to offset/limit the single row returned
                # by count(*).  Because we're wrapping the query, we don't care
                # about the column values.
                col_str = '1'
            else:
                col_str = 'count(*)'
        else:
            col_str = self._seqtostr(self.columns)
        table_str = self._seqtostr(self.tables)
        join_str = self._joinstr()
        clause_str = self._seqtostr(self.clauses, sep=')\n   AND (')
        if clause_str:
            clause_str = ' WHERE (' + clause_str + ')'
        order_str = self._order()
        offset_str = self._optstr('offset')
        limit_str = self._optstr('limit')

        query = query % locals()
        if self.opts.get('countOnly') and \
           (self.opts.get('offset') or self.opts.get('limit')):
            query = 'SELECT count(*)\nFROM (' + query + ') numrows'
        return query

    def __repr__(self):
        return '<QueryProcessor: columns=%r, aliases=%r, tables=%r, joins=%r, clauses=%r, values=%r, opts=%r>' % \
               (self.columns, self.aliases, self.tables, self.joins, self.clauses, self.values, self.opts)

    def _seqtostr(self, seq, sep=', '):
        if seq:
            return sep.join(seq)
        else:
            return ''

    def _joinstr(self):
        if not self.joins:
            return ''
        result = ''
        for join in self.joins:
            if result:
                result += '\n'
            if re.search(r'\bjoin\b', join, re.IGNORECASE):
                # The join clause already contains the word 'join',
                # so don't prepend 'JOIN' to it
                result += '  ' + join
            else:
                result += '  JOIN ' + join
        return result

    def _order(self):
        # Don't bother sorting if we're just counting
        if self.opts.get('countOnly'):
            return ''
        order = self.opts.get('order')
        if order:
            if order.startswith('-'):
                order = order[1:]
                direction = ' DESC'
            else:
                direction = ''
            # Check if we're ordering by alias first
            orderCol = self.colsByAlias.get(order)
            if orderCol:
                pass
            elif order in self.columns:
                orderCol = order
            else:
                raise StandardError, 'invalid order: ' + order
            return 'ORDER BY ' + orderCol + direction
        else:
            return ''

    def _optstr(self, optname):
        optval = self.opts.get(optname)
        if optval:
            return '%s %i' % (optname.upper(), optval)
        else:
            return ''

    def execute(self):
        query = str(self)
        if self.opts.get('countOnly'):
            return _singleValue(query, self.values, strict=True)
        elif self.opts.get('asList'):
            return _fetchMulti(query, self.values)
        else:
            return _multiRow(query, self.values, (self.aliases or self.columns))

    def executeOne(self):
        results = self.execute()
        if isinstance(results, list):
            if len(results) > 0:
                return results[0]
            else:
                return None
        return results

def _applyQueryOpts(results, queryOpts):
    """
    Apply queryOpts to results in the same way QueryProcessor would.
    results is a list of maps.
    queryOpts is a map which may contain the following fields:
      countOnly
      order
      offset
      limit

    Note: asList is supported by QueryProcessor but not by this method.
    We don't know the original query order, and so don't have a way to
    return a useful list.  asList should be handled by the caller.
    """
    if queryOpts is None:
        queryOpts = {}
    if queryOpts.get('order'):
        order = queryOpts['order']
        reverse = False
        if order.startswith('-'):
            order = order[1:]
            reverse = True
        results.sort(key=lambda o: o[order])
        if reverse:
            results.reverse()
    if queryOpts.get('offset'):
        results = results[queryOpts['offset']:]
    if queryOpts.get('limit'):
        results = results[:queryOpts['limit']]
    if queryOpts.get('countOnly'):
        return len(results)
    else:
        return results

#
# Policy Test Handlers


class OperationTest(koji.policy.MatchTest):
    """Checks operation against glob patterns"""
    name = 'operation'
    field = 'operation'

class PackageTest(koji.policy.MatchTest):
    """Checks package against glob patterns"""
    name = 'package'
    field = '_package'
    def run(self, data):
        #we need to find the package name from the base data
        data[self.field] = get_build(data['build'])['name']
        return super(PackageTest, self).run(data)

class TagTest(koji.policy.MatchTest):
    name = 'tag'
    field = '_tagname'

    def get_tag(self, data):
        """extract the tag to test against from the data

        return None if there is no tag to test
        """
        tag = data.get('tag')
        if tag is None:
            return None
        return get_tag(tag, strict=False)

    def run(self, data):
        #we need to find the tag name from the base data
        tinfo = self.get_tag(data)
        if tinfo is None:
            return False
        data[self.field] = tinfo['name']
        return super(TagTest, self).run(data)

class FromTagTest(TagTest):
    name = 'fromtag'
    def get_tag(self, data):
        tag = data.get('fromtag')
        if tag is None:
            return None
        return get_tag(tag, strict=False)

class HasTagTest(koji.policy.BaseSimpleTest):
    """Check to see if build (currently) has a given tag"""
    name = 'hastag'
    def run(self, data):
        tags = context.handlers.call('listTags', build=data['build'])
        #True if any of these tags match any of the patterns
        args = self.str.split()[1:]
        for tag in tags:
            for pattern in args:
                if fnmatch.fnmatch(tag['name'], pattern):
                    return True
        #otherwise...
        return False

class SkipTagTest(koji.policy.BaseSimpleTest):
    """Check for the skip_tag option

    For policies regarding build tasks (e.g. build_from_srpm)
    """
    name = 'skip_tag'
    def run(self, data):
        return bool(data.get('skip_tag'))

class BuildTagTest(koji.policy.BaseSimpleTest):
    """Check the build tag of the build

    If build_tag is not provided in policy data, it is determined by the
    buildroots of the component rpms
    """
    name = 'buildtag'
    def run(self, data):
        args = self.str.split()[1:]
        if data.has_key('build_tag'):
            tagname = get_tag(data['build_tag'])
            for pattern in args:
                if fnmatch.fnmatch(tagname, pattern):
                    return True
            #else
            return False
        elif data.has_key('build'):
            #determine build tag from buildroots
            #in theory, we should find only one unique build tag
            #it is possible that some rpms could have been imported later and hence
            #not have a buildroot.
            #or if the entire build was imported, there will be no buildroots
            rpms = context.handlers.call('listRPMs', buildID=data['build'])
            for rpminfo in rpms:
                if rpminfo['buildroot_id'] is None:
                    continue
                tagname = get_buildroot(rpminfo['buildroot_id'])['tag_name']
                for pattern in args:
                    if fnmatch.fnmatch(tagname, pattern):
                        return True
            #otherwise...
            return False
        else:
            return False

class ImportedTest(koji.policy.BaseSimpleTest):
    """Check if any part of a build was imported

    This is determined by checking the buildroots of the rpms
    True if any rpm lacks a buildroot (strict)"""
    name = 'imported'
    def run(self, data):
        rpms = context.handlers.call('listRPMs', buildID=data['build'])
        #no test args
        for rpminfo in rpms:
            if rpminfo['buildroot_id'] is None:
                return True
        #otherwise...
        return False

class IsBuildOwnerTest(koji.policy.BaseSimpleTest):
    """Check if user owns the build"""
    name = "is_build_owner"
    def run(self, data):
        build = get_build(data['build'])
        owner = get_user(build['owner_id'])
        user = get_user(data['user_id'])
        if owner['id'] == user['id']:
            return True
        if owner['usertype'] == koji.USERTYPES['GROUP']:
            # owner is a group, check to see if user is a member
            if owner['id'] in koji.auth.get_user_groups(user['id']):
                return True
        #otherwise...
        return False

class UserInGroupTest(koji.policy.BaseSimpleTest):
    """Check if user is in group(s)

    args are treated as patterns and matched against group name
    true is user is in /any/ matching group
    """
    name = "user_in_group"
    def run(self, data):
        user = get_user(data['user_id'])
        groups = koji.auth.get_user_groups(user['id'])
        args = self.str.split()[1:]
        for group_id, group in groups.iteritems():
            for pattern in args:
                if fnmatch.fnmatch(group, pattern):
                    return True
        #otherwise...
        return False

class HasPermTest(koji.policy.BaseSimpleTest):
    """Check if user has permission(s)

    args are treated as patterns and matched against permission name
    true is user has /any/ matching permission
    """
    name = "has_perm"
    def run(self, data):
        user = get_user(data['user_id'])
        perms = koji.auth.get_user_perms(user['id'])
        args = self.str.split()[1:]
        for perm in perms:
            for pattern in args:
                if fnmatch.fnmatch(perm, pattern):
                    return True
        #otherwise...
        return False

class SourceTest(koji.policy.MatchTest):
    """Match build source

    This is not the cleanest, since we have to crack open the task parameters
    True if build source matches any of the supplied patterns
    """
    name = "source"
    field = '_source'
    def run(self, data):
        if data.has_key('source'):
            data[self.field] = data['source']
        elif data.has_key('build'):
            #crack open the build task
            build = get_build(data['build'])
            if build['task_id'] is None:
                #imported, no source to match against
                return False
            task = Task(build['task_id'])
            params = task.getRequest()
            #signature is (src, target, opts=None)
            data[self.field] = params[0]
        else:
            return False
        return super(SourceTest, self).run(data)

class PolicyTest(koji.policy.BaseSimpleTest):
    """Test named policy

    The named policy must exist
    Returns True is the policy results in an action of:
        yes, true, allow
    Otherwise returns False
    (Also returns False if there are no matches in the policy)
    Watch out for loops
    """
    name = 'policy'

    def __init__(self, str):
        super(PolicyTest, self).__init__(str)
        self.depth = 0
        # this is used to detect loops. Note that each test in a ruleset is
        # a distinct instance of its test class. So this value is particular
        # to a given appearance of a policy check in a ruleset.

    def run(self, data):
        args = self.str.split()[1:]
        if self.depth != 0:
            #LOOP!
            raise koji.GenericError, "encountered policy loop at %s" % self.str
        ruleset = context.policy.get(args[0])
        if not ruleset:
            raise koji.GenericError, "no such policy: %s" % args[0]
        self.depth += 1
        result = ruleset.apply(data)
        self.depth -= 1
        if result is None:
            return False
        else:
            return result.lower() in ('yes', 'true', 'allow')


def check_policy(name, data, default='deny', strict=False):
    """Check data against the named policy

    This assumes the policy actions consist of:
        allow
        deny

    Returns a pair (access, reason)
        access: True if the policy result is allow, false otherwise
        reason: reason for the access
    If strict is True, will raise ActionNotAllowed if the action is not 'allow'
    """
    ruleset = context.policy.get(name)
    if not ruleset:
        if context.opts.get('MissingPolicyOk'):
            # for backwards compatibility, this is the default
            result = "allow"
        else:
            result = "deny"
        reason = "missing policy"
    else:
        result = ruleset.apply(data)
        if result is None:
            result = default
        reason = ruleset.last_rule()
    if context.opts.get('KojiDebug', False):
        log_error("policy %(name)s gave %(result)s, reason: %(reason)s" % locals())
    if result.lower() == 'allow':
        return True, reason
    if not strict:
        return False, reason
    err_str = "policy violation"
    if context.opts.get('KojiDebug') or context.opts.get('VerbosePolicy'):
        err_str += " -- %s" % reason
    raise koji.ActionNotAllowed, err_str

def assert_policy(name, data, default='deny'):
    """Enforce the named policy

    This assumes the policy actions consist of:
        allow
        deny
    Raises ActionNotAllowed if policy result is not allow
    """
    check_policy(name, data, default=default, strict=True)

def rpmdiff(basepath, rpmlist):
    "Diff the first rpm in the list against the rest of the rpms."
    if len(rpmlist) < 2:
        return
    first_rpm = rpmlist[0]
    for other_rpm in rpmlist[1:]:
        # ignore differences in file size, md5sum, and mtime
        # (files may have been generated at build time and contain
        #  embedded dates or other insignificant differences)
        args = ['/usr/libexec/koji-hub/rpmdiff',
                '--ignore', 'S', '--ignore', '5',
                '--ignore', 'T',
                os.path.join(basepath, first_rpm),
                os.path.join(basepath, other_rpm)]
        proc = subprocess.Popen(args,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                close_fds=True)
        output = proc.communicate()[0]
        status = proc.wait()
        if os.WIFSIGNALED(status) or \
                (os.WEXITSTATUS(status) != 0):
            raise koji.BuildError, 'mismatch when analyzing %s, rpmdiff output was:\n%s' % \
                (os.path.basename(first_rpm), output)

def importImageInternal(task_id, filename, filesize, arch, mediatype, hash, rpmlist):
    """
    Import image info and the listing into the database, and move an image
    to the final resting place. The filesize may be reported as a string if it
    exceeds the 32-bit signed integer limit. This function will convert it if
    need be. Not called for scratch images.
    """

    #sanity checks
    host = Host()
    host.verify()
    task = Task(task_id)
    task.assertHost(host.id)

    imageinfo = {}
    imageinfo['id'] = _singleValue("""SELECT nextval('imageinfo_id_seq')""")
    imageinfo['taskid'] = task_id
    imageinfo['filename'] = filename
    imageinfo['filesize'] = int(filesize)
    imageinfo['arch'] = arch
    imageinfo['mediatype'] = mediatype
    imageinfo['hash'] = hash
    q = """INSERT INTO imageinfo (id,task_id,filename,filesize,
           arch,mediatype,hash)
           VALUES (%(id)i,%(taskid)i,%(filename)s,%(filesize)i,
           %(arch)s,%(mediatype)s,%(hash)s)
        """
    _dml(q, imageinfo)

    q = """INSERT INTO imageinfo_listing (image_id,rpm_id)
           VALUES (%(image_id)i,%(rpm_id)i)"""

    rpm_ids = []
    for an_rpm in rpmlist:
        location = an_rpm.get('location')
        if location:
            data = add_external_rpm(an_rpm, location, strict=False)
        else:
            data = get_rpm(an_rpm, strict=True)
        rpm_ids.append(data['id'])

    image_id = imageinfo['id']
    for rpm_id in rpm_ids:
        _dml(q, locals())

    return image_id

def moveImageResults(task_id, image_id, arch):
    """
    Move the image file from the work/task directory into its more
    permanent resting place. This shouldn't be called for scratch images.
    """
    source_path = os.path.join(koji.pathinfo.work(),
                               koji.pathinfo.taskrelpath(task_id))
    final_path = os.path.join(koji.pathinfo.imageFinalPath(),
                              koji.pathinfo.livecdRelPath(image_id))
    log_path = os.path.join(final_path, 'data', 'logs', arch)
    if os.path.exists(final_path) or os.path.exists(log_path):
        raise koji.GenericError, "Error moving LiveCD image: the final " + \
            "destination already exists!"
    koji.ensuredir(final_path)
    koji.ensuredir(log_path)

    src_files = os.listdir(source_path)
    got_iso = False
    for fname in src_files:
        if fname.endswith('.iso'):
            got_iso = True
            dest_path = final_path
        else:
            dest_path = log_path
        os.rename(os.path.join(source_path, fname),
                  os.path.join(dest_path, fname))
        os.symlink(os.path.join(dest_path, fname),
                   os.path.join(source_path, fname))

    if not got_iso:
        raise koji.GenericError, "Could not move the iso to the final destination!"

#
# XMLRPC Methods
#
class RootExports(object):
    '''Contains functions that are made available via XMLRPC'''

    def buildFromCVS(self, url, tag):
        raise koji.Deprecated
        #return make_task('buildFromCVS',[url, tag])

    def build(self, src, target, opts=None, priority=None, channel=None):
        """Create a build task

        priority: the amount to increase (or decrease) the task priority, relative
                  to the default priority; higher values mean lower priority; only
                  admins have the right to specify a negative priority here
        channel: the channel to allocate the task to
        Returns the task id
        """
        if not opts:
            opts = {}
        taskOpts = {}
        if priority:
            if priority < 0:
                if not context.session.hasPerm('admin'):
                    raise koji.ActionNotAllowed, 'only admins may create high-priority tasks'
            taskOpts['priority'] = koji.PRIO_DEFAULT + priority
        if channel:
            taskOpts['channel'] = channel
        return make_task('build',[src, target, opts],**taskOpts)

    def chainBuild(self, srcs, target, opts=None, priority=None, channel=None):
        """Create a chained build task for building sets of packages in order

        srcs: list of pkg lists, ie [[src00, src01, src03],[src20],[src30,src31],...]
              where each of the top-level lists gets built and a new repo is created
              before the next list is built.
        target: build target
        priority: the amount to increase (or decrease) the task priority, relative
                  to the default priority; higher values mean lower priority; only
                  admins have the right to specify a negative priority here
        channel: the channel to allocate the task to
        Returns a list of all the dependent task ids
        """
        if not opts:
            opts = {}
        taskOpts = {}
        if priority:
            if priority < 0:
                if not context.session.hasPerm('admin'):
                    raise koji.ActionNotAllowed, 'only admins may create high-priority tasks'
            taskOpts['priority'] = koji.PRIO_DEFAULT + priority
        if channel:
            taskOpts['channel'] = channel

        return make_task('chainbuild',[srcs,target,opts],**taskOpts)

    # Create the livecd task. Called from handle_spin_livecd in the client.
    #
    def livecd (self, arch, target, ksfile, opts=None, priority=None):
        """
        Create a live CD image using a kickstart file and group package list.
        """

        context.session.assertPerm('livecd')

        taskOpts = {'channel': 'livecd'}
        taskOpts['arch'] = arch
        if priority:
            if priority < 0:
                if not context.session.hasPerm('admin'):
                    raise koji.ActionNotAllowed, \
                               'only admins may create high-priority tasks'

            taskOpts['priority'] = koji.PRIO_DEFAULT + priority

        return make_task('createLiveCD', [arch, target, ksfile, opts],
                         **taskOpts)

    # Database access to get imageinfo values. Used in parts of kojiweb.
    #
    def getImageInfo(self, imageID=None, taskID=None, strict=False):
        """
        Return the row from imageinfo given an image_id OR build_root_id.
        It is an error if neither are specified, and image_id takes precedence.
        Filesize will be reported as a string if it exceeds the 32-bit signed
        integer limit.
        """
        tables = ['imageinfo']
        fields = ['imageinfo.id', 'filename', 'filesize', 'imageinfo.arch', 'mediatype',
                  'imageinfo.task_id', 'buildroot.id', 'hash']
        aliases = ['id', 'filename', 'filesize', 'arch', 'mediatype', 'task_id',
                   'br_id', 'hash']
        joins = ['buildroot ON imageinfo.task_id = buildroot.task_id']
        if imageID:
            clauses = ['imageinfo.id = %(imageID)i']
        elif taskID:
            clauses = ['imageinfo.task_id = %(taskID)i']
        else:
            raise koji.GenericError, 'either imageID or taskID must be specified'

        query = QueryProcessor(columns=fields, tables=tables, clauses=clauses,
                               values=locals(), joins=joins, aliases=aliases)
        ret = query.executeOne()

        if strict and not ret:
            if imageID:
                raise koji.GenericError, 'no image with ID: %i' % imageID
            else:
                raise koji.GenericError, 'no image for task ID: %i' % taskID

        # additional tweaking
        if ret:
            # Always return filesize as a string instead of an int so XMLRPC doesn't
            # complain about 32-bit overflow
            ret['filesize'] = str(ret['filesize'])
        return ret

    def hello(self,*args):
        return "Hello World"

    def fault(self):
        "debugging. raise an error"
        raise Exception, "test exception"

    def error(self):
        "debugging. raise an error"
        raise koji.GenericError, "test error"

    def echo(self,*args):
        return args

    def getAPIVersion(self):
        return koji.API_VERSION

    def showSession(self):
        return "%s" % context.session

    def showOpts(self):
        context.session.assertPerm('admin')
        return "%r" % context.opts

    def getEvent(self, id):
        """
        Get information about the event with the given id.

        A map will be returned with the following keys:
          - id (integer): id of the event
          - ts (float): timestamp the event was created, in
                        seconds since the epoch

        If no event with the given id exists, an error will be raised.
        """
        fields = ('id', 'ts')
        values = {'id': id}
        q = """SELECT id, EXTRACT(EPOCH FROM time) FROM events
                WHERE id = %(id)i"""
        return _singleRow(q, values, fields, strict=True)

    def getLastEvent(self, before=None):
        """
        Get the id and timestamp of the last event recorded in the system.
        Events are usually created as the result of a configuration change
        in the database.

        If "before" (int or float) is specified, return the last event
        that occurred before that time (in seconds since the epoch).
        If there is no event before the given time, an error will be raised.

        Note that due to differences in precision between the database and python,
        this method can return an event with a timestamp the same or slightly higher
        (by a few microseconds) than the value of "before" provided.  Code using this
        method should check that the timestamp returned is in fact lower than the parameter.
        When trying to find information about a specific event, the getEvent() method
        should be used.
        """
        fields = ('id', 'ts')
        values = {}
        q = """SELECT id, EXTRACT(EPOCH FROM time) FROM events"""
        if before is not None:
            if not isinstance(before, (int, long, float)):
                raise koji.GenericError, 'invalid type for before: %s' % type(before)
            # use the repr() conversion because it retains more precision than the
            # string conversion
            q += """ WHERE EXTRACT(EPOCH FROM time) < %(before)r"""
            values['before'] = before
        q += """ ORDER BY id DESC LIMIT 1"""
        return _singleRow(q, values, fields, strict=True)

    def makeTask(self,*args,**opts):
        #this is mainly for debugging
        #only an admin can make arbitrary tasks
        context.session.assertPerm('admin')
        return make_task(*args,**opts)

    def uploadFile(self, path, name, size, md5sum, offset, data):
        #path: the relative path to upload to
        #name: the name of the file
        #size: size of contents (bytes)
        #md5: md5sum (hex digest) of contents
        #data: base64 encoded file contents
        #offset: the offset of the chunk
        # files can be uploaded in chunks, if so the md5 and size describe
        # the chunk rather than the whole file. the offset indicates where
        # the chunk belongs
        # the special offset -1 is used to indicate the final chunk
        if not context.session.logged_in:
            raise koji.GenericError, 'you must be logged-in to upload a file'
        contents = base64.decodestring(data)
        del data
        # we will accept offset and size as strings to work around xmlrpc limits
        offset = koji.decode_int(offset)
        size = koji.decode_int(size)
        if offset != -1:
            if size is not None:
                if size != len(contents): return False
            if md5sum is not None:
                if md5sum != md5_constructor(contents).hexdigest():
                    return False
        uploadpath = koji.pathinfo.work()
        #XXX - have an incoming dir and move after upload complete
        # SECURITY - ensure path remains under uploadpath
        path = os.path.normpath(path)
        if path.startswith('..'):
            raise koji.GenericError, "Upload path not allowed: %s" % path
        udir = "%s/%s" % (uploadpath,path)
        koji.ensuredir(udir)
        fn = "%s/%s" % (udir,name)
        try:
            st = os.lstat(fn)
        except OSError, e:
            if e.errno == errno.ENOENT:
                pass
            else:
                raise
        else:
            if not stat.S_ISREG(st.st_mode):
                raise koji.GenericError, "destination not a file: %s" % fn
            elif offset == 0:
                #first chunk, so file should not exist yet
                if not fn.endswith('.log'):
                    # but we allow .log files to be uploaded multiple times to support
                    # realtime log-file viewing
                    raise koji.GenericError, "file already exists: %s" % fn
        fd = os.open(fn, os.O_RDWR | os.O_CREAT, 0666)
        # log_error("fd=%r" %fd)
        try:
            if offset == 0 or (offset == -1 and size == len(contents)):
                #truncate file
                fcntl.lockf(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                try:
                    os.ftruncate(fd, 0)
                    # log_error("truncating fd %r to 0" %fd)
                finally:
                    fcntl.lockf(fd, fcntl.LOCK_UN)
            if offset == -1:
                os.lseek(fd,0,2)
            else:
                os.lseek(fd,offset,0)
            #write contents
            fcntl.lockf(fd, fcntl.LOCK_EX|fcntl.LOCK_NB, len(contents), 0, 2)
            try:
                os.write(fd, contents)
                # log_error("wrote contents")
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN, len(contents), 0, 2)
            if offset == -1:
                if size is not None:
                    #truncate file
                    fcntl.lockf(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    try:
                        os.ftruncate(fd, size)
                        # log_error("truncating fd %r to size %r" % (fd,size))
                    finally:
                        fcntl.lockf(fd, fcntl.LOCK_UN)
                if md5sum is not None:
                    #check final md5sum
                    sum = md5_constructor()
                    fcntl.lockf(fd, fcntl.LOCK_SH|fcntl.LOCK_NB)
                    try:
                        # log_error("checking md5sum")
                        os.lseek(fd,0,0)
                        while True:
                            block = os.read(fd, 819200)
                            if not block: break
                            sum.update(block)
                        if md5sum != sum.hexdigest():
                            # log_error("md5sum did not match")
                            #os.close(fd)
                            return False
                    finally:
                        fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return True

    def downloadTaskOutput(self, taskID, fileName, offset=0, size=-1):
        """Download the file with the given name, generated by the task with the
        given ID."""
        if '..' in fileName or '/' in fileName:
            raise koji.GenericError, 'Invalid file name: %s' % fileName
        filePath = '%s/%s/%s' % (koji.pathinfo.work(), koji.pathinfo.taskrelpath(taskID), fileName)
        filePath = os.path.normpath(filePath)
        if not os.path.isfile(filePath):
            raise koji.GenericError, 'no file "%s" output by task %i' % (fileName, taskID)
        # Let the caller handler any IO or permission errors
        f = file(filePath, 'r')
        if isinstance(offset, int):
            if offset > 0:
                f.seek(offset, 0)
            elif offset < 0:
                f.seek(offset, 2)
        contents = f.read(size)
        f.close()
        return base64.encodestring(contents)

    def listTaskOutput(self, taskID, stat=False):
        """List the files generated by the task with the given ID.  This
        will usually include one or more RPMs, and one or more log files.
        If the task did not generate any files, or the output directory
        for the task no longer exists, return an empty list.

        If stat is True, return a map of filename -> stat_info where stat_info
        is a map containing the values of the st_* attributes returned by
        os.stat()."""
        taskDir = '%s/%s' % (koji.pathinfo.work(), koji.pathinfo.taskrelpath(taskID))
        if stat:
            ret = {}
        else:
            ret = []
        if os.path.isdir(taskDir):
            output = os.listdir(taskDir)
            if stat:
                for filename in output:
                    stat_info = os.stat(os.path.join(taskDir, filename))
                    stat_map = {}
                    for attr in dir(stat_info):
                        if attr.startswith('st_'):
                            if attr == 'st_size':
                                stat_map[attr] = str(getattr(stat_info, attr))
                            else:
                                stat_map[attr] = getattr(stat_info, attr)
                    ret[filename] = stat_map
            else:
                ret = output
        return ret

    createTag = staticmethod(create_tag)
    editTag = staticmethod(old_edit_tag)
    editTag2 = staticmethod(edit_tag)
    deleteTag = staticmethod(delete_tag)

    createExternalRepo = staticmethod(create_external_repo)
    listExternalRepos = staticmethod(get_external_repos)
    getExternalRepo = staticmethod(get_external_repo)
    editExternalRepo = staticmethod(edit_external_repo)
    deleteExternalRepo = staticmethod(delete_external_repo)

    def addExternalRepoToTag(self, tag_info, repo_info, priority):
        """Add an external repo to a tag"""
        # wrap the local method so we don't expose the event parameter
        add_external_repo_to_tag(tag_info, repo_info, priority)

    def removeExternalRepoFromTag(self, tag_info, repo_info):
        """Remove an external repo from a tag"""
        # wrap the local method so we don't expose the event parameter
        remove_external_repo_from_tag(tag_info, repo_info)

    editTagExternalRepo = staticmethod(edit_tag_external_repo)
    getTagExternalRepos = staticmethod(get_tag_external_repos)
    getExternalRepoList = staticmethod(get_external_repo_list)

    importBuildInPlace = staticmethod(import_build_in_place)
    resetBuild = staticmethod(reset_build)

    untaggedBuilds = staticmethod(untagged_builds)
    tagHistory = staticmethod(tag_history)

    buildMap = staticmethod(build_map)
    deleteBuild = staticmethod(delete_build)
    def buildReferences(self, build, limit=None):
        return build_references(get_build(build, strict=True)['id'], limit)

    def createEmptyBuild(self, name, version, release, epoch, owner=None):
        context.session.assertPerm('admin')
        data = { 'name' : name, 'version' : version, 'release' : release,
                 'epoch' : epoch }
        if owner is not None:
            data['owner'] = owner
        return new_build(data)

    def importRPM(self, path, basename):
        """Import an RPM into the database.

        The file must be uploaded first.
        """
        context.session.assertPerm('admin')
        uploadpath = koji.pathinfo.work()
        fn = "%s/%s/%s" %(uploadpath,path,basename)
        if not os.path.exists(fn):
            raise koji.GenericError, "No such file: %s" % fn
        rpminfo = import_rpm(fn)
        import_rpm_file(fn,rpminfo['build'],rpminfo)
        add_rpm_sig(rpminfo['id'], koji.rip_rpm_sighdr(fn))

    def addExternalRPM(self, rpminfo, external_repo, strict=True):
        """Import an external RPM

        This call is mainly for testing. Normal access will be through
        a host call"""
        context.session.assertPerm('admin')
        add_external_rpm(rpminfo, external_repo, strict=strict)

    def tagBuildBypass(self,tag,build,force=False):
        """Tag a build without running post checks or notifications

        This is a short circuit function for imports.
        Admin permission required.

        Tagging with a locked tag is not allowed unless force is true.
        Retagging is not allowed unless force is true. (retagging changes the order
        of entries will affect which build is the latest)
        """
        context.session.assertPerm('admin')
        _tag_build(tag, build, force=force)

    def tagBuild(self,tag,build,force=False,fromtag=None):
        """Request that a build be tagged

        The force option will attempt to force the action in the event of:
            - tag locked
            - missing permission
            - package not in list for tag
        The force option is really only effect for admins

        If fromtag is specified, this becomes a move operation.

        This call creates a task to do some of the heavy lifting
        The return value is the task id
        """
        #first some lookups and basic sanity checks
        build = get_build(build, strict=True)
        tag = get_tag(tag, strict=True)
        if fromtag:
            fromtag_id = get_tag_id(fromtag, strict=True)
        else:
            fromtag_id = None
        pkg_id = build['package_id']
        tag_id = tag['id']
        build_id = build['id']
        # note: we're just running the quick checks now so we can fail
        #       early if appropriate, rather then waiting for the task
        # Make sure package is on the list for this tag
        pkgs = readPackageList(tagID=tag_id, pkgID=pkg_id, inherit=True)
        pkg_error = None
        if not pkgs.has_key(pkg_id):
            pkg_error = "Package %s not in list for %s" % (build['name'], tag['name'])
        elif pkgs[pkg_id]['blocked']:
            pkg_error = "Package %s blocked in %s" % (build['name'], tag['name'])
        policy_data = {'tag' : tag_id, 'build' : build_id, 'fromtag' : fromtag_id}
        policy_data['user_id'] = context.session.user_id
        if fromtag is None:
            policy_data['operation'] = 'tag'
        else:
            policy_data['operation'] = 'move'
        #don't check policy for admins using force
        if not force or not context.session.hasPerm('admin'):
            assert_policy('tag', policy_data)
        #XXX - we're running this check twice, here and in host.tagBuild (called by the task)
        if pkg_error:
            if force and context.session.hasPerm('admin'):
                pkglist_add(tag_id,pkg_id,force=True,block=False)
            else:
                raise koji.TagError, pkg_error
        #access check
        assert_tag_access(tag_id,user_id=None,force=force)
        if fromtag:
            assert_tag_access(fromtag_id,user_id=None,force=force)
        #spawn the tagging tasks (it performs more thorough checks)
        return make_task('tagBuild', [tag_id, build_id, force, fromtag_id], priority=10)

    def untagBuild(self,tag,build,strict=True,force=False):
        """Untag a build

        Unlike tagBuild, this does not create a task
        No return value"""
        #we can't staticmethod this one -- we're limiting the options
        user_id = context.session.user_id
        tag_id = get_tag(tag, strict=True)['id']
        build_id = get_build(build, strict=True)['id']
        policy_data = {'tag' : None, 'build' : build_id, 'fromtag' : tag_id}
        policy_data['user_id'] = context.session.user_id
        policy_data['operation'] = 'untag'
        try:
            #don't check policy for admins using force
            if not force or not context.session.hasPerm('admin'):
                assert_policy('tag', policy_data)
            _untag_build(tag,build,strict=strict,force=force)
            tag_notification(True, None, tag, build, user_id)
        except Exception, e:
            exctype, value = sys.exc_info()[:2]
            tag_notification(False, None, tag, build, user_id, False, "%s: %s" % (exctype, value))
            raise

    def untagBuildBypass(self, tag, build, strict=True, force=False):
        """Untag a build without any checks or notifications

        Admins only. Intended for syncs/imports.

        Unlike tagBuild, this does not create a task
        No return value"""
        context.session.assertPerm('admin')
        _untag_build(tag, build, strict=strict, force=force)

    def moveBuild(self,tag1,tag2,build,force=False):
        """Move a build from tag1 to tag2

        Returns the task id of the task performing the move"""
        return self.tagBuild(tag2,build,force=force,fromtag=tag1)

    def moveAllBuilds(self, tag1, tag2, package, force=False):
        """Move all builds of a package from tag1 to tag2 in the correct order

        Returns the task id of the task performing the move"""

        #lookups and basic sanity checks
        pkg_id = get_package_id(package, strict=True)
        tag1_id = get_tag_id(tag1, strict=True)
        tag2_id = get_tag_id(tag2, strict=True)

        # note: we're just running the quick checks now so we can fail
        #       early if appropriate, rather then waiting for the task
        # Make sure package is on the list for the tag we're adding it to
        pkgs = readPackageList(tagID=tag2_id, pkgID=pkg_id, inherit=True)
        pkg_error = None
        if not pkgs.has_key(pkg_id):
            pkg_error = "Package %s not in list for tag %s" % (package, tag2)
        elif pkgs[pkg_id]['blocked']:
            pkg_error = "Package %s blocked in tag %s" % (package, tag2)
        if pkg_error:
            if force and context.session.hasPerm('admin'):
                pkglist_add(tag2_id,pkg_id,force=True,block=False)
            else:
                raise koji.TagError, pkg_error

        #access check
        assert_tag_access(tag1_id,user_id=None,force=force)
        assert_tag_access(tag2_id,user_id=None,force=force)

        build_list = readTaggedBuilds(tag1_id, package=package)
        # we want 'ORDER BY tag_listing.create_event ASC' not DESC so reverse
        build_list.reverse()

        #policy check
        policy_data = {'tag' : tag2, 'fromtag' : tag1, 'operation' : 'move'}
        policy_data['user_id'] = context.session.user_id
        #don't check policy for admins using force
        if not force or not context.session.hasPerm('admin'):
            for build in build_list:
                policy_data['build'] = build
                assert_policy('tag', policy_data)
                #XXX - we're running this check twice, here and in host.tagBuild (called by the task)

        wait_on = []
        tasklist = []
        for build in build_list:
            task_id = make_task('dependantTask', [wait_on, [['tagBuild', [tag2_id, build['id'], force, tag1_id], {'priority':15}]]])
            wait_on = [task_id]
            log_error("\nMade Task: %s\n" % task_id)
            tasklist.append(task_id)
        return tasklist

    def fixTags(self):
        """A fix for incomplete tag import, adds tag_config entries

        Note the query will only add the tag_config entries if there are
        no other tag_config entries, so it will not 'undelete' any tags"""
        c = context.cnx.cursor()
        q = """
        INSERT INTO tag_config(tag_id,arches,perm_id,locked)
        SELECT id,'i386 ia64 ppc ppc64 s390 s390x sparc sparc64 x86_64',NULL,False
        FROM tag LEFT OUTER JOIN tag_config ON tag.id = tag_config.tag_id
        WHERE revoke_event IS NULL AND active IS NULL;
        """
        context.commit_pending = True
        c.execute(q)

    def listTags(self, build=None, package=None, queryOpts=None):
        """List tags.  If build is specified, only return tags associated with the
        given build.  If package is specified, only return tags associated with the
        specified package.  If neither is specified, return all tags.  Build can be
        either an integer ID or a string N-V-R.  Package can be either an integer ID
        or a string name.  Only one of build and package may be specified.  Returns
        a list of maps.  Each map contains keys:
          - id
          - name
          - perm_id
          - perm
          - arches
          - locked

        If package is specified, each map will also contain:
          - owner_id
          - owner_name
          - blocked
          - extra_arches
        """
        if build is not None and package is not None:
            raise koji.GenericError, 'only one of build and package may be specified'

        tables = ['tag_config']
        joins = ['tag ON tag.id = tag_config.tag_id',
                 'LEFT OUTER JOIN permissions ON tag_config.perm_id = permissions.id']
        fields = ['tag.id', 'tag.name', 'tag_config.perm_id', 'permissions.name',
                  'tag_config.arches', 'tag_config.locked']
        aliases = ['id', 'name', 'perm_id', 'perm',
                   'arches', 'locked']
        clauses = ['tag_config.active = true']

        if build is not None:
            # lookup build id
            buildinfo = get_build(build)
            if not buildinfo:
                raise koji.GenericError, 'invalid build: %s' % build
            joins.append('tag_listing ON tag.id = tag_listing.tag_id')
            clauses.append('tag_listing.active = true')
            clauses.append('tag_listing.build_id = %(buildID)i')
            buildID = buildinfo['id']
        elif package is not None:
            packageinfo = self.getPackage(package)
            if not packageinfo:
                raise koji.GenericError, 'invalid package: %s' % package
            fields.extend(['users.id', 'users.name', 'tag_packages.blocked', 'tag_packages.extra_arches'])
            aliases.extend(['owner_id', 'owner_name', 'blocked', 'extra_arches'])
            joins.append('tag_packages ON tag.id = tag_packages.tag_id')
            clauses.append('tag_packages.active = true')
            clauses.append('tag_packages.package_id = %(packageID)i')
            joins.append('users ON tag_packages.owner = users.id')
            packageID = packageinfo['id']

        query = QueryProcessor(columns=fields, aliases=aliases, tables=tables,
                               joins=joins, clauses=clauses, values=locals(),
                               opts=queryOpts)
        return query.execute()

    getBuild = staticmethod(get_build)

    def getChangelogEntries(self, buildID=None, taskID=None, filepath=None, author=None, before=None, after=None, queryOpts=None):
        """Get changelog entries for the build with the given ID,
           or for the rpm generated by the given task at the given path

        - author: only return changelogs with a matching author
        - before: only return changelogs from before the given date (in UTC)
                  (a datetime object, a string in the 'YYYY-MM-DD HH24:MI:SS format, or integer seconds
                   since the epoch)
        - after: only return changelogs from after the given date (in UTC)
                 (a datetime object, a string in the 'YYYY-MM-DD HH24:MI:SS format, or integer seconds
                  since the epoch)
        - queryOpts: query options used by the QueryProcessor

        If "order" is not specified in queryOpts, results will be returned in reverse chronological
        order.

        Results will be returned as a list of maps with 'date', 'author', and 'text' keys.
        If there are no results, an empty list will be returned.
        """
        if queryOpts is None:
            queryOpts = {}
        if queryOpts.get('order') in ('date', '-date'):
            # use a numeric sort on the timestamp instead of an alphabetic sort on the
            # date string
            queryOpts['order'] = queryOpts['order'].replace('date', 'date_ts')
        if buildID:
            build_info = get_build(buildID)
            if not build_info:
                return _applyQueryOpts([], queryOpts)
            srpms = self.listRPMs(buildID=build_info['id'], arches='src')
            if not srpms:
                return _applyQueryOpts([], queryOpts)
            srpm_info = srpms[0]
            srpm_path = os.path.join(koji.pathinfo.build(build_info), koji.pathinfo.rpm(srpm_info))
        elif taskID:
            if not filepath:
                raise koji.GenericError, 'filepath must be spcified with taskID'
            if filepath.startswith('/') or '../' in filepath:
                raise koji.GenericError, 'invalid filepath: %s' % filepath
            srpm_path = os.path.join(koji.pathinfo.work(),
                                     koji.pathinfo.taskrelpath(taskID),
                                     filepath)
        else:
            raise koji.GenericError, 'either buildID or taskID and filepath must be specified'

        if not os.path.exists(srpm_path):
            return _applyQueryOpts([], queryOpts)

        if before:
            if isinstance(before, datetime.datetime):
                before = calendar.timegm(before.utctimetuple())
            elif isinstance(before, (str, unicode)):
                before = koji.util.parseTime(before)
            elif isinstance(before, (int, long)):
                pass
            else:
                raise koji.GenericError, 'invalid type for before: %s' % type(before)

        if after:
            if isinstance(after, datetime.datetime):
                after = calendar.timegm(after.utctimetuple())
            elif isinstance(after, (str, unicode)):
                after = koji.util.parseTime(after)
            elif isinstance(after, (int, long)):
                pass
            else:
                raise koji.GenericError, 'invalid type for after: %s' % type(after)

        results = []

        fields = koji.get_header_fields(srpm_path, ['changelogtime', 'changelogname', 'changelogtext'])
        for (cltime, clname, cltext) in zip(fields['changelogtime'], fields['changelogname'],
                                            fields['changelogtext']):
            cldate = datetime.datetime.fromtimestamp(cltime).isoformat(' ')
            clname = koji.fixEncoding(clname)
            cltext = koji.fixEncoding(cltext)

            if author and author != clname:
                continue
            if before and not cltime < before:
                continue
            if after and not cltime > after:
                continue

            if queryOpts.get('asList'):
                results.append([cldate, clname, cltext])
            else:
                results.append({'date': cldate, 'date_ts': cltime, 'author': clname, 'text': cltext})

        return _applyQueryOpts(results, queryOpts)

    def cancelBuild(self, buildID):
        """Cancel the build with the given buildID

        If the build is associated with a task, cancel the task as well.
        Return True if the build was successfully canceled, False if not."""
        build = get_build(buildID)
        if build == None:
            return False
        if build['owner_id'] != context.session.user_id:
            if not context.session.hasPerm('admin'):
                raise koji.ActionNotAllowed, 'Cannot cancel build, not owner'
        return cancel_build(build['id'])

    def assignTask(self,task_id,host,force=False):
        """Assign a task to a host

        Specify force=True to assign a non-free task
        """
        context.session.assertPerm('admin')
        task = Task(task_id)
        host = get_host(host,strict=True)
        task.assign(host['id'],force)

    def freeTask(self,task_id):
        """Free a task"""
        context.session.assertPerm('admin')
        task = Task(task_id)
        task.free()

    def cancelTask(self,task_id,recurse=True):
        """Cancel a task"""
        task = Task(task_id)
        if not task.verifyOwner() and not task.verifyHost():
            if not context.session.hasPerm('admin'):
                raise koji.ActionNotAllowed, 'Cannot cancel task, not owner'
        #non-admins can also use cancelBuild
        task.cancel(recurse=recurse)

    def cancelTaskFull(self,task_id,strict=True):
        """Cancel a task and all tasks in its group"""
        context.session.assertPerm('admin')
        #non-admins can use cancelBuild or cancelTask
        Task(task_id).cancelFull(strict=strict)

    def cancelTaskChildren(self,task_id):
        """Cancel a task's children, but not the task itself"""
        task = Task(task_id)
        if not task.verifyOwner() and not task.verifyHost():
            if not context.session.hasPerm('admin'):
                raise koji.ActionNotAllowed, 'Cannot cancel task, not owner'
        task.cancelChildren()

    def setTaskPriority(self, task_id, priority, recurse=True):
        """Set task priority"""
        context.session.assertPerm('admin')
        task = Task(task_id)
        task.setPriority(priority, recurse=recurse)

    def listTagged(self,tag,event=None,inherit=False,prefix=None,latest=False,package=None,owner=None):
        """List builds tagged with tag"""
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        results = readTaggedBuilds(tag,event,inherit=inherit,latest=latest,package=package,owner=owner)
        if prefix:
            prefix = prefix.lower()
            results = [build for build in results if build['package_name'].lower().startswith(prefix)]
        return results

    def listTaggedRPMS(self,tag,event=None,inherit=False,latest=False,package=None,arch=None,rpmsigs=False,owner=None):
        """List rpms and builds within tag"""
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        return readTaggedRPMS(tag,event=event,inherit=inherit,latest=latest,package=package,arch=arch,rpmsigs=rpmsigs,owner=owner)

    def listBuilds(self, packageID=None, userID=None, taskID=None, prefix=None, state=None,
                   createdBefore=None, createdAfter=None,
                   completeBefore=None, completeAfter=None, queryOpts=None):
        """List package builds.
        If packageID is specified, restrict the results to builds of the specified package.
        If userID is specified, restrict the results to builds owned by the given user.
        If taskID is specfied, restrict the results to builds with the given task ID.  If taskID is -1,
           restrict the results to builds with a non-null taskID.
        If prefix is specified, restrict the results to builds whose package name starts with that
        prefix.
        If createdBefore and/or createdAfter are specified, restrict the results to builds whose
        creation_time is before and/or after the given time.
        If completeBefore and/or completeAfter are specified, restrict the results to builds whose
        completion_time is before and/or after the given time.
        The time may be specified as a floating point value indicating seconds since the Epoch (as
        returned by time.time()) or as a string in ISO format ('YYYY-MM-DD HH24:MI:SS').
        One or more of packageID, userID, and taskID may be specified.

        Returns a list of maps.  Each map contains the following keys:

          - build_id
          - version
          - release
          - epoch
          - state
          - package_id
          - package_name
          - name (same as package_name)
          - nvr (synthesized for sorting purposes)
          - owner_id
          - owner_name
          - creation_event_id
          - creation_time
          - completion_time
          - task_id

        If no builds match, an empty list is returned.
        """
        fields = (('build.id', 'build_id'), ('build.version', 'version'), ('build.release', 'release'),
                  ('build.epoch', 'epoch'), ('build.state', 'state'), ('build.completion_time', 'completion_time'),
                  ('events.id', 'creation_event_id'), ('events.time', 'creation_time'), ('build.task_id', 'task_id'),
                  ('package.id', 'package_id'), ('package.name', 'package_name'), ('package.name', 'name'),
                  ("package.name || '-' || build.version || '-' || build.release", 'nvr'),
                  ('users.id', 'owner_id'), ('users.name', 'owner_name'))

        tables = ['build']
        joins = ['events ON build.create_event = events.id',
                 'package ON build.pkg_id = package.id',
                 'users ON build.owner = users.id']
        clauses = []
        if packageID != None:
            clauses.append('package.id = %(packageID)i')
        if userID != None:
            clauses.append('users.id = %(userID)i')
        if taskID != None:
            if taskID == -1:
                clauses.append('build.task_id IS NOT NULL')
            else:
                clauses.append('build.task_id = %(taskID)i')
        if prefix:
            clauses.append("package.name ilike %(prefix)s || '%%'")
        if state != None:
            clauses.append('build.state = %(state)i')
        if createdBefore:
            if not isinstance(createdBefore, str):
                createdBefore = datetime.datetime.fromtimestamp(createdBefore).isoformat(' ')
            clauses.append('events.time < %(createdBefore)s')
        if createdAfter:
            if not isinstance(createdAfter, str):
                createdAfter = datetime.datetime.fromtimestamp(createdAfter).isoformat(' ')
            clauses.append('events.time > %(createdAfter)s')
        if completeBefore:
            if not isinstance(completeBefore, str):
                completeBefore = datetime.datetime.fromtimestamp(completeBefore).isoformat(' ')
            clauses.append('build.completion_time < %(completeBefore)s')
        if completeAfter:
            if not isinstance(completeAfter, str):
                completeAfter = datetime.datetime.fromtimestamp(completeAfter).isoformat(' ')
            clauses.append('build.completion_time > %(completeAfter)s')

        query = QueryProcessor(columns=[pair[0] for pair in fields],
                               aliases=[pair[1] for pair in fields],
                               tables=tables, joins=joins, clauses=clauses,
                               values=locals(), opts=queryOpts)

        return query.execute()

    def getLatestBuilds(self,tag,event=None,package=None):
        """List latest builds for tag (inheritance enabled)"""
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        return readTaggedBuilds(tag,event,inherit=True,latest=True,package=package)

    def getLatestRPMS(self, tag, package=None, arch=None, event=None, rpmsigs=False):
        """List latest RPMS for tag (inheritance enabled)"""
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        return readTaggedRPMS(tag, package=package, arch=arch, event=event,inherit=True,latest=True, rpmsigs=rpmsigs)

    def getAverageBuildDuration(self, package):
        """Get the average duration of a build of the given package.
        Returns a floating-point value indicating the
        average number of seconds the package took to build.  If the package
        has never been built, return None."""
        packageID = get_package_id(package)
        if not packageID:
            return None
        st_complete = koji.BUILD_STATES['COMPLETE']
        query = """SELECT EXTRACT(epoch FROM avg(build.completion_time - events.time))
                     FROM build
                     JOIN events ON build.create_event = events.id
                     WHERE build.pkg_id = %(packageID)i
                       AND build.state = %(st_complete)i
                       AND build.task_id IS NOT NULL"""

        return _singleValue(query, locals())

    packageListAdd = staticmethod(pkglist_add)
    packageListRemove = staticmethod(pkglist_remove)
    packageListBlock = staticmethod(pkglist_block)
    packageListUnblock = staticmethod(pkglist_unblock)
    packageListSetOwner = staticmethod(pkglist_setowner)
    packageListSetArches = staticmethod(pkglist_setarches)

    groupListAdd = staticmethod(grplist_add)
    groupListRemove = staticmethod(grplist_remove)
    groupListBlock = staticmethod(grplist_block)
    groupListUnblock = staticmethod(grplist_unblock)

    groupPackageListAdd = staticmethod(grp_pkg_add)
    groupPackageListRemove = staticmethod(grp_pkg_remove)
    groupPackageListBlock = staticmethod(grp_pkg_block)
    groupPackageListUnblock = staticmethod(grp_pkg_unblock)

    groupReqListAdd = staticmethod(grp_req_add)
    groupReqListRemove = staticmethod(grp_req_remove)
    groupReqListBlock = staticmethod(grp_req_block)
    groupReqListUnblock = staticmethod(grp_req_unblock)

    getTagGroups = staticmethod(readTagGroups)

    checkTagAccess = staticmethod(check_tag_access)

    getGlobalInheritance = staticmethod(readGlobalInheritance)

    def getInheritanceData(self,tag):
        """Return inheritance data for tag"""
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        return readInheritanceData(tag)

    def setInheritanceData(self,tag,data,clear=False):
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        context.session.assertPerm('admin')
        return writeInheritanceData(tag,data,clear=clear)

    def getFullInheritance(self,tag,event=None,reverse=False,stops={},jumps={}):
        if not isinstance(tag,int):
            #lookup tag id
            tag = get_tag_id(tag,strict=True)
        for mapping in [stops, jumps]:
            for key in mapping.keys():
                mapping[int(key)] = mapping[key]
        return readFullInheritance(tag,event,reverse,stops,jumps)

    def listRPMs(self, buildID=None, buildrootID=None, imageID=None, componentBuildrootID=None, hostID=None, arches=None, queryOpts=None):
        """List RPMS.  If buildID, imageID and/or buildrootID are specified,
        restrict the list of RPMs to only those RPMs that are part of that
        build, or were built in that buildroot.  If componentBuildrootID is specified,
        restrict the list to only those RPMs that will get pulled into that buildroot
        when it is used to build another package.  A list of maps is returned, each map
        containing the following keys:

        - id
        - name
        - version
        - release
        - nvr (synthesized for sorting purposes)
        - arch
        - epoch
        - payloadhash
        - size
        - buildtime
        - build_id
        - buildroot_id
        - external_repo_id
        - external_repo_name

        If componentBuildrootID is specified, two additional keys will be included:
        - component_buildroot_id
        - is_update

        If no build has the given ID, or the build generated no RPMs,
        an empty list is returned."""
        fields = [('rpminfo.id', 'id'), ('rpminfo.name', 'name'), ('rpminfo.version', 'version'),
                  ('rpminfo.release', 'release'),
                  ("rpminfo.name || '-' || rpminfo.version || '-' || rpminfo.release", 'nvr'),
                  ('rpminfo.arch', 'arch'),
                  ('rpminfo.epoch', 'epoch'), ('rpminfo.payloadhash', 'payloadhash'),
                  ('rpminfo.size', 'size'), ('rpminfo.buildtime', 'buildtime'),
                  ('rpminfo.build_id', 'build_id'), ('rpminfo.buildroot_id', 'buildroot_id'),
                  ('rpminfo.external_repo_id', 'external_repo_id'),
                  ('external_repo.name', 'external_repo_name'),
                 ]
        joins = ['external_repo ON rpminfo.external_repo_id = external_repo.id']
        clauses = []

        if buildID != None:
            clauses.append('rpminfo.build_id = %(buildID)i')
        if buildrootID != None:
            clauses.append('rpminfo.buildroot_id = %(buildrootID)i')
        if componentBuildrootID != None:
            fields.append(('buildroot_listing.buildroot_id as component_buildroot_id',
                           'component_buildroot_id'))
            fields.append(('buildroot_listing.is_update', 'is_update'))
            joins.append('buildroot_listing ON rpminfo.id = buildroot_listing.rpm_id')
            clauses.append('buildroot_listing.buildroot_id = %(componentBuildrootID)i')

        # image specific constraints
        if imageID != None:
           clauses.append('imageinfo_listing.image_id = %(imageID)i')
           joins.append('imageinfo_listing ON rpminfo.id = imageinfo_listing.rpm_id')

        if hostID != None:
            joins.append('buildroot ON rpminfo.buildroot_id = buildroot.id')
            clauses.append('buildroot.host_id = %(hostID)i')
        if arches != None:
            if isinstance(arches, list) or isinstance(arches, tuple):
                clauses.append('rpminfo.arch IN %(arches)s')
            elif isinstance(arches, str):
                clauses.append('rpminfo.arch = %(arches)s')
            else:
                raise koji.GenericError, 'invalid type for "arches" parameter: %s' % type(arches)

        query = QueryProcessor(columns=[f[0] for f in fields], aliases=[f[1] for f in fields],
                               tables=['rpminfo'], joins=joins, clauses=clauses,
                               values=locals(), opts=queryOpts)
        return query.execute()

    def listBuildRPMs(self,build):
        """Get information about all the RPMs generated by the build with the given
        ID.  A list of maps is returned, each map containing the following keys:

        - id
        - name
        - version
        - release
        - arch
        - epoch
        - payloadhash
        - size
        - buildtime
        - build_id
        - buildroot_id

        If no build has the given ID, or the build generated no RPMs, an empty list is returned."""
        if not isinstance(build, int):
            #lookup build id
            build = self.findBuildID(build)
        return self.listRPMs(buildID=build)

    getRPM = staticmethod(get_rpm)

    def getRPMDeps(self, rpmID, depType=None, queryOpts=None):
        """Return dependency information about the RPM with the given ID.
        If depType is specified, restrict results to dependencies of the given type.
        Otherwise, return all dependency information.  A list of maps will be returned,
        each with the following keys:
        - name
        - version
        - flags
        - type

        If there is no RPM with the given ID, or the RPM has no dependency information,
        an empty list will be returned.
        """
        if queryOpts is None:
            queryOpts = {}
        rpm_info = get_rpm(rpmID)
        if not rpm_info or not rpm_info['build_id']:
            return _applyQueryOpts([], queryOpts)
        build_info = get_build(rpm_info['build_id'])
        rpm_path = os.path.join(koji.pathinfo.build(build_info), koji.pathinfo.rpm(rpm_info))
        if not os.path.exists(rpm_path):
            return _applyQueryOpts([], queryOpts)

        results = []

        for dep_name in ['REQUIRE','PROVIDE','CONFLICT','OBSOLETE']:
            dep_id = getattr(koji, 'DEP_' + dep_name)
            if depType is None or depType == dep_id:
                fields = koji.get_header_fields(rpm_path, [dep_name + 'NAME',
                                                           dep_name + 'VERSION',
                                                           dep_name + 'FLAGS'])
                for (name, version, flags) in zip(fields[dep_name + 'NAME'],
                                                  fields[dep_name + 'VERSION'],
                                                  fields[dep_name + 'FLAGS']):
                    if queryOpts.get('asList'):
                        results.append([name, version, flags, dep_id])
                    else:
                        results.append({'name': name, 'version': version, 'flags': flags, 'type': dep_id})

        return _applyQueryOpts(results, queryOpts)

    def listRPMFiles(self, rpmID, queryOpts=None):
        """List files associated with the RPM with the given ID.  A list of maps
        will be returned, each with the following keys:
        - name
        - digest
        - md5 (alias for digest)
        - digest_algo
        - size
        - flags

        If there is no RPM with the given ID, or that RPM contains no files,
        an empty list will be returned."""
        if queryOpts is None:
            queryOpts = {}
        rpm_info = get_rpm(rpmID)
        if not rpm_info or not rpm_info['build_id']:
            return _applyQueryOpts([], queryOpts)
        build_info = get_build(rpm_info['build_id'])
        rpm_path = os.path.join(koji.pathinfo.build(build_info), koji.pathinfo.rpm(rpm_info))
        if not os.path.exists(rpm_path):
            return _applyQueryOpts([], queryOpts)

        results = []
        hdr = koji.get_rpm_header(rpm_path)
        fields = koji.get_header_fields(hdr, ['filenames', 'filemd5s', 'filesizes', 'fileflags'])
        digest_algo = koji.util.filedigestAlgo(hdr)

        for (name, digest, size, flags) in zip(fields['filenames'], fields['filemd5s'],
                                           fields['filesizes'], fields['fileflags']):
            if queryOpts.get('asList'):
                results.append([name, digest, size, flags, digest_algo])
            else:
                results.append({'name': name, 'digest': digest, 'digest_algo': digest_algo,
                                'md5': digest, 'size': size, 'flags': flags})

        return _applyQueryOpts(results, queryOpts)

    def getRPMFile(self, rpmID, filename):
        """
        Get info about the file in the given RPM with the given filename.
        A map will be returned with the following keys:
        - rpm_id
        - name
        - digest
        - md5 (alias for digest)
        - digest_algo
        - size
        - flags

        If no such file exists, an empty map will be returned.
        """
        rpm_info = get_rpm(rpmID)
        if not rpm_info or not rpm_info['build_id']:
            return {}
        build_info = get_build(rpm_info['build_id'])
        rpm_path = os.path.join(koji.pathinfo.build(build_info), koji.pathinfo.rpm(rpm_info))
        if not os.path.exists(rpm_path):
            return {}

        hdr = koji.get_rpm_header(rpm_path)
        # use filemd5s for backward compatibility
        fields = koji.get_header_fields(hdr, ['filenames', 'filemd5s', 'filesizes', 'fileflags'])
        digest_algo = koji.util.filedigestAlgo(hdr)

        i = 0
        for name in fields['filenames']:
            if name == filename:
                return {'rpm_id': rpm_info['id'], 'name': name, 'digest': fields['filemd5s'][i],
                        'digest_algo': digest_algo, 'md5': fields['filemd5s'][i],
                        'size': fields['filesizes'][i], 'flags': fields['fileflags'][i]}
            i += 1
        return {}

    def getRPMHeaders(self, rpmID=None, taskID=None, filepath=None, headers=None):
        """
        Get the requested headers from the rpm.  Header names are case-insensitive.
        If a header is requested that does not exist an exception will be raised.
        Returns a map of header names to values.  If the specified ID
        is not valid or the rpm does not exist on the file system, an empty map
        will be returned.
        """
        if not headers:
            headers = []
        if rpmID:
            rpm_info = get_rpm(rpmID)
            if not rpm_info or not rpm_info['build_id']:
                return {}
            build_info = get_build(rpm_info['build_id'])
            rpm_path = os.path.join(koji.pathinfo.build(build_info), koji.pathinfo.rpm(rpm_info))
            if not os.path.exists(rpm_path):
                return {}
        elif taskID:
            if not filepath:
                raise koji.GenericError, 'filepath must be specified with taskID'
            if filepath.startswith('/') or '../' in filepath:
                raise koji.GenericError, 'invalid filepath: %s' % filepath
            rpm_path = os.path.join(koji.pathinfo.work(),
                                    koji.pathinfo.taskrelpath(taskID),
                                    filepath)
        else:
            raise koji.GenericError, 'either rpmID or taskID and filepath must be specified'

        return koji.get_header_fields(rpm_path, headers)

    queryRPMSigs = staticmethod(query_rpm_sigs)
    writeSignedRPM = staticmethod(write_signed_rpm)

    def addRPMSig(self, an_rpm, data):
        """Store a signature header for an rpm

        data: the signature header encoded as base64
        """
        context.session.assertPerm('sign')
        return add_rpm_sig(an_rpm, base64.decodestring(data))

    findBuildID = staticmethod(find_build_id)
    getTagID = staticmethod(get_tag_id)
    getTag = staticmethod(get_tag)

    def getPackageID(self,name):
        c=context.cnx.cursor()
        q="""SELECT id FROM package WHERE name=%(name)s"""
        c.execute(q,locals())
        r=c.fetchone()
        if not r:
            return None
        return r[0]

    getPackage = staticmethod(lookup_package)

    def listPackages(self, tagID=None, userID=None, pkgID=None, prefix=None, inherited=False, with_dups=False, event=None):
        """List if tagID and/or userID is specified, limit the
        list to packages belonging to the given user or with the
        given tag.

        A list of maps is returned.  Each map contains the
        following keys:

        - package_id
        - package_name

        If tagID, userID, or pkgID are specified, the maps will also contain the
        following keys.

        - tag_id
        - tag_name
        - owner_id
        - owner_name
        - extra_arches
        - blocked
        """
        if tagID is None and userID is None and pkgID is None:
            query = """SELECT id, name from package"""
            results = _multiRow(query,{},('package_id', 'package_name'))
        else:
            if tagID is not None:
                tagID = get_tag_id(tagID,strict=True)
            if userID is not None:
                userID = get_user(userID,strict=True)['id']
            if pkgID is not None:
                pkgID = get_package_id(pkgID,strict=True)
            result_list = readPackageList(tagID=tagID, userID=userID, pkgID=pkgID,
                                          inherit=inherited, with_dups=with_dups,
                                          event=event).values()
            if with_dups:
                # when with_dups=True, readPackageList returns a list of list of dicts
                # convert it to a list of dicts for consistency
                results = []
                for result in result_list:
                    results.extend(result)
            else:
                results = result_list

        if prefix:
            prefix = prefix.lower()
            results = [package for package in results if package['package_name'].lower().startswith(prefix)]

        return results

    def checkTagPackage(self,tag,pkg):
        """Check that pkg is in the list for tag. Returns true/false"""
        tag_id = get_tag_id(tag,strict=False)
        pkg_id = get_package_id(pkg,strict=False)
        if pkg_id is None or tag_id is None:
            return False
        pkgs = readPackageList(tagID=tag_id, pkgID=pkg_id, inherit=True)
        if not pkgs.has_key(pkg_id):
            return False
        else:
            #still might be blocked
            return not pkgs[pkg_id]['blocked']

    def getPackageConfig(self,tag,pkg,event=None):
        """Get config for package in tag"""
        tag_id = get_tag_id(tag,strict=False)
        pkg_id = get_package_id(pkg,strict=False)
        if pkg_id is None or tag_id is None:
            return None
        pkgs = readPackageList(tagID=tag_id, pkgID=pkg_id, inherit=True, event=event)
        return pkgs.get(pkg_id,None)

    getUser = staticmethod(get_user)

    def grantPermission(self, userinfo, permission):
        """Grant a permission to a user"""
        context.session.assertPerm('admin')
        user_id = get_user(userinfo,strict=True)['id']
        perm = lookup_perm(permission, strict=True)
        perm_id = perm['id']
        if perm['name'] in koji.auth.get_user_perms(user_id):
            raise koji.GenericError, 'user %s already has permission: %s' % (userinfo, perm['name'])
        insert = """INSERT INTO user_perms (user_id, perm_id)
        VALUES (%(user_id)i, %(perm_id)i)"""
        _dml(insert, locals())

    def createUser(self, username, status=None, krb_principal=None):
        """Add a user to the database"""
        context.session.assertPerm('admin')
        if get_user(username):
            raise koji.GenericError, 'user already exists: %s' % username
        if krb_principal and get_user(krb_principal):
            raise koji.GenericError, 'user with this Kerberos principal already exists: %s' % krb_principal
        
        return context.session.createUser(username, status=status, krb_principal=krb_principal)

    def enableUser(self, username):
        """Enable logins by the specified user"""
        user = get_user(username)
        if not user:
            raise koji.GenericError, 'unknown user: %s' % username
        set_user_status(user, koji.USER_STATUS['NORMAL'])
    
    def disableUser(self, username):
        """Disable logins by the specified user"""
        user = get_user(username)
        if not user:
            raise koji.GenericError, 'unknown user: %s' % username
        set_user_status(user, koji.USER_STATUS['BLOCKED'])
    
    #group management calls
    newGroup = staticmethod(new_group)
    addGroupMember = staticmethod(add_group_member)
    dropGroupMember = staticmethod(drop_group_member)
    getGroupMembers = staticmethod(get_group_members)

    def listUsers(self, userType=koji.USERTYPES['NORMAL'], prefix=None, queryOpts=None):
        """List all users in the system.
        type can be either koji.USERTYPES['NORMAL']
        or koji.USERTYPES['HOST'].  Returns a list of maps with the
        following keys:

        - id
        - name
        - status
        - usertype
        - krb_principal

        If no users of the specified
        type exist, return an empty list."""
        fields = ('id', 'name', 'status', 'usertype', 'krb_principal')
        clauses = ['usertype = %(userType)i']
        if prefix:
            clauses.append("name ilike %(prefix)s || '%%'")
        query = QueryProcessor(columns=fields, tables=('users',), clauses=clauses,
                               values=locals(), opts=queryOpts)
        return query.execute()

    def getBuildConfig(self,tag,event=None):
        """Return build configuration associated with a tag"""
        taginfo = get_tag(tag,strict=True,event=event)
        arches = taginfo['arches']
        if arches is None:
            #follow inheritance for arches
            order = readFullInheritance(taginfo['id'],event=event)
            for link in order:
                if link['noconfig']:
                    continue
                arches = get_tag(link['parent_id'],strict=True,event=event)['arches']
                if arches is not None:
                    taginfo['arches'] = arches
                    break
        return taginfo

    def getRepo(self,tag,state=None):
        if isinstance(tag,int):
            id = tag
        else:
            id = get_tag_id(tag,strict=True)

        fields = ['repo.id', 'repo.state', 'repo.create_event', 'events.time', 'EXTRACT(EPOCH FROM events.time)']
        aliases = ['id', 'state', 'create_event', 'creation_time', 'create_ts']
        joins = ['events ON repo.create_event = events.id']
        clauses = ['repo.tag_id = %(id)i']
        if state is None:
            state = koji.REPO_READY
        clauses.append('repo.state = %(state)s' )

        query = QueryProcessor(columns=fields, aliases=aliases,
                               tables=['repo'], joins=joins, clauses=clauses,
                               values=locals(),
                               opts={'order': '-creation_time', 'limit': 1})
        return query.executeOne()

    repoInfo = staticmethod(repo_info)
    getActiveRepos = staticmethod(get_active_repos)

    def newRepo(self, tag, event=None, src=False, debuginfo=False):
        """Create a newRepo task. returns task id"""
        if context.session.hasPerm('regen-repo'):
            pass
        else:
            context.session.assertPerm('repo')
        opts = {}
        if event is not None:
            opts['event'] = event
        if src:
            opts['src'] = True
        if debuginfo:
            opts['debuginfo'] = True
        args = koji.encode_args(tag, **opts)
        return make_task('newRepo', args, priority=15, channel='createrepo')

    def repoExpire(self, repo_id):
        """mark repo expired"""
        context.session.assertPerm('repo')
        repo_expire(repo_id)

    def repoDelete(self, repo_id):
        """Attempt to mark repo deleted, return number of references

        If the number of references is nonzero, no change is made
        Does not remove from disk"""
        context.session.assertPerm('repo')
        return repo_delete(repo_id)

    def repoProblem(self, repo_id):
        """mark repo as broken"""
        context.session.assertPerm('repo')
        repo_problem(repo_id)

    def debugFunction(self, name, *args, **kwargs):
        # This is potentially dangerous, so it must be explicitly enabled
        allowed = context.opts.get('EnableFunctionDebug', False)
        if not allowed:
            raise koji.ActionNotAllowed, 'This call is not enabled'
        context.session.assertPerm('admin')
        func = globals().get(name)
        if callable(func):
            return func(*args, **kwargs)
        else:
            raise koji.GenericError, 'Unable to find function: %s' % name

    tagChangedSinceEvent = staticmethod(tag_changed_since_event)
    createBuildTarget = staticmethod(create_build_target)
    editBuildTarget = staticmethod(edit_build_target)
    deleteBuildTarget = staticmethod(delete_build_target)
    getBuildTargets = staticmethod(get_build_targets)

    def getBuildTarget(self, info, event=None, strict=False):
        """Return the build target with the given name or ID.
        If there is no matching build target, return None."""
        targets = get_build_targets(info=info, event=event)
        if len(targets) == 1:
            return targets[0]
        else:
            if strict:
                raise koji.GenericError, 'No matching build target found: %s' % info
            else:
                return None

    def taskFinished(self,taskId):
        task = Task(taskId)
        return task.isFinished()

    def getTaskRequest(self, taskId):
        task = Task(taskId)
        return task.getRequest()

    def getTaskResult(self, taskId):
        task = Task(taskId)
        return task.getResult()

    def getTaskInfo(self, task_id, request=False):
        """Get information about a task"""
        single = True
        if isinstance(task_id, list) or isinstance(task_id, tuple):
            single = False
        else:
            task_id = [task_id]
        ret = [Task(id).getInfo(False, request) for id in task_id]
        if single:
            return ret[0]
        else:
            return ret

    def getTaskChildren(self, task_id):
        """Return a list of the children
        of the Task with the given ID."""
        task = Task(task_id)
        return task.getChildren()

    def getTaskDescendents(self, task_id, request=False):
        """Get all descendents of the task with the given ID.
        Return a map of task_id -> list of child tasks.  If the given
        task has no descendents, the map will contain a single elements
        mapping the given task ID to an empty list.  Map keys will be strings
        representing integers, due to limitations in xmlrpclib.  If "request"
        is true, the parameters sent with the xmlrpc request will be decoded and
        included in the map."""
        task = Task(task_id)
        return get_task_descendents(task, request=request)

    def listTasks(self, opts=None, queryOpts=None):
        """Return list of tasks filtered by options

        Options(dictionary):
            option[type]: meaning
            arch[list]: limit to tasks for given arches
            state[list]: limit to tasks of given state
            owner[int]: limit to tasks owned by the user with the given ID
            host_id[int]: limit to tasks running on the host with the given ID
            parent[int]: limit to tasks with the given parent
            decode[bool]: whether or not xmlrpc data in the 'request' and 'result'
                          fields should be decoded; defaults to False
            method[str]: limit to tasks of the given method
            completeBefore[float or str]: limit to tasks whose completion_time is before
                                         the given date, in either float (seconds since the epoch)
                                         or str (ISO) format
            completeAfter[float or str]: limit to tasks whose completion_time is after
                                         the given date, in either float (seconds since the epoch)
                                         or str (ISO) format
        """
        if not opts:
            opts = {}

        tables = ['task']
        joins = ['users ON task.owner = users.id']
        fields = ('task.id','state','create_time','completion_time','channel_id',
                  'host_id','parent','label','waiting','awaited','owner','method',
                  'arch','priority','weight','request','result', 'users.name', 'users.usertype')
        aliases = ('id','state','create_time','completion_time','channel_id',
                   'host_id','parent','label','waiting','awaited','owner','method',
                   'arch','priority','weight','request','result', 'owner_name', 'owner_type')

        conditions = []
        for f in ['arch','state']:
            if opts.has_key(f):
                conditions.append('%s IN %%(%s)s' % (f, f))
        for f in ['owner', 'host_id', 'parent']:
            if opts.has_key(f):
                if opts[f] is None:
                    conditions.append('%s IS NULL' % f)
                else:
                    conditions.append('%s = %%(%s)i' % (f, f))
        if opts.has_key('method'):
            conditions.append('method = %(method)s')
        if opts.get('completeBefore') != None:
            completeBefore = opts['completeBefore']
            if not isinstance(completeBefore, str):
                opts['completeBefore'] = datetime.datetime.fromtimestamp(completeBefore).isoformat(' ')
            conditions.append('completion_time < %(completeBefore)s')
        if opts.get('completeAfter') != None:
            completeAfter = opts['completeAfter']
            if not isinstance(completeAfter, str):
                opts['completeAfter'] = datetime.datetime.fromtimestamp(completeAfter).isoformat(' ')
            conditions.append('completion_time > %(completeAfter)s')

        query = QueryProcessor(columns=fields, aliases=aliases, tables=tables, joins=joins,
                               clauses=conditions, values=opts, opts=queryOpts)
        tasks = query.execute()
        if queryOpts and (queryOpts.get('countOnly') or queryOpts.get('asList')):
            # Either of the above options makes us unable to easily the decode
            # the xmlrpc data
            return tasks

        if opts.get('decode'):
            for task in tasks:
                # decode xmlrpc data
                for f in ('request','result'):
                    if task[f]:
                        try:
                            if task[f].find('<?xml', 0, 10) == -1:
                                #handle older base64 encoded data
                                task[f] = base64.decodestring(task[f])
                            data, method = xmlrpclib.loads(task[f])
                        except xmlrpclib.Fault, fault:
                            data = fault
                        task[f] = data
        return tasks

    def taskReport(self, owner=None):
        """Return data on active or recent tasks"""
        fields = (
            ('task.id','id'),
            ('task.state','state'),
            ('task.create_time','create_time'),
            ('task.completion_time','completion_time'),
            ('task.channel_id','channel_id'),
            ('channels.name','channel'),
            ('task.host_id','host_id'),
            ('host.name','host'),
            ('task.parent','parent'),
            ('task.waiting','waiting'),
            ('task.awaited','awaited'),
            ('task.method','method'),
            ('task.arch','arch'),
            ('task.priority','priority'),
            ('task.weight','weight'),
            ('task.owner','owner_id'),
            ('users.name','owner'),
            ('build.id','build_id'),
            ('package.name','build_name'),
            ('build.version','build_version'),
            ('build.release','build_release'),
        )
        q = """SELECT %s FROM task
        JOIN channels ON task.channel_id = channels.id
        JOIN users ON task.owner = users.id
        LEFT OUTER JOIN host ON task.host_id = host.id
        LEFT OUTER JOIN build ON build.task_id = task.id
        LEFT OUTER JOIN package ON build.pkg_id = package.id
        WHERE (task.state NOT IN (%%(CLOSED)d,%%(CANCELED)d,%%(FAILED)d)
            OR NOW() - task.create_time < '1 hour'::interval)
            """ % ','.join([f[0] for f in fields])
        if owner:
            q += """AND users.id = %s
            """ % get_user(owner, strict=True)['id']
        q += """ORDER BY priority,create_time
        """
        #XXX hard-coded interval
        c = context.cnx.cursor()
        c.execute(q,koji.TASK_STATES)
        return [dict(zip([f[1] for f in fields],row)) for row in c.fetchall()]

    def resubmitTask(self, taskID):
        """Retry a canceled or failed task, using the same parameter as the original task.
        The logged-in user must be the owner of the original task or an admin."""
        task = Task(taskID)
        if not (task.isCanceled() or task.isFailed()):
            raise koji.GenericError, 'only canceled or failed tasks may be resubmitted'
        taskInfo = task.getInfo()
        if taskInfo['parent'] != None:
            raise koji.GenericError, 'only top-level tasks may be resubmitted'
        if not (context.session.user_id == taskInfo['owner'] or self.hasPerm('admin')):
            raise koji.GenericError, 'only the task owner or an admin may resubmit a task'

        args = task.getRequest()
        channel = get_channel(taskInfo['channel_id'], strict=True)

        return make_task(taskInfo['method'], args, arch=taskInfo['arch'], channel=channel['name'], priority=taskInfo['priority'])

    def addHost(self, hostname, arches, krb_principal=None):
        """Add a host to the database"""
        context.session.assertPerm('admin')
        if get_host(hostname):
            raise koji.GenericError, 'host already exists: %s' % hostname
        q = """SELECT id FROM channels WHERE name = 'default'"""
        default_channel = _singleValue(q)
        if krb_principal is None:
            fmt = context.opts.get('HostPrincipalFormat')
            if fmt:
                krb_principal = fmt % hostname
        #users entry
        userID = context.session.createUser(hostname, usertype=koji.USERTYPES['HOST'],
                                            krb_principal=krb_principal)
        #host entry
        hostID = _singleValue("SELECT nextval('host_id_seq')", strict=True)
        arches = " ".join(arches)
        insert = """INSERT INTO host (id, user_id, name, arches)
        VALUES (%(hostID)i, %(userID)i, %(hostname)s, %(arches)s)"""
        _dml(insert, locals())
        #host_channels entry
        insert = """INSERT INTO host_channels (host_id, channel_id)
        VALUES (%(hostID)i, %(default_channel)i)"""
        _dml(insert, locals())
        return hostID

    def enableHost(self, hostname):
        """Mark a host as enabled"""
        set_host_enabled(hostname, True)

    def disableHost(self, hostname):
        """Mark a host as disabled"""
        set_host_enabled(hostname, False)

    getHost = staticmethod(get_host)
    addHostToChannel = staticmethod(add_host_to_channel)
    removeHostFromChannel = staticmethod(remove_host_from_channel)

    def listHosts(self, arches=None, channelID=None, ready=None, enabled=None, userID=None, queryOpts=None):
        """Get a list of hosts.  "arches" is a list of string architecture
        names, e.g. ['i386', 'ppc64'].  If one of the arches associated with a given
        host appears in the list, it will be included in the results.  If "ready" and "enabled"
        are specified, only hosts with the given value for the respective field will
        be included."""
        fields = ('id', 'user_id', 'name', 'arches', 'task_load',
                  'capacity', 'ready', 'enabled')

        clauses = []
        joins = []
        if arches != None:
            # include the regex constraints below so we can match 'ppc' without
            # matching 'ppc64'
            if not (isinstance(arches, list) or isinstance(arches, tuple)):
                arches = [arches]
            archClause = [r"""arches ~ E'\\m%s\\M'""" % arch for arch in arches]
            clauses.append('(' + ' OR '.join(archClause) + ')')
        if channelID != None:
            joins.append('host_channels on host.id = host_channels.host_id')
            clauses.append('host_channels.channel_id = %(channelID)i')
        if ready != None:
            if ready:
                clauses.append('ready is true')
            else:
                clauses.append('ready is false')
        if enabled != None:
            if enabled:
                clauses.append('enabled is true')
            else:
                clauses.append('enabled is false')
        if userID != None:
            clauses.append('user_id = %(userID)i')

        query = QueryProcessor(columns=fields, tables=['host'],
                               joins=joins, clauses=clauses,
                               values=locals(), opts=queryOpts)
        return query.execute()

    def getLastHostUpdate(self, hostID):
        """Return the latest update timestampt for the host

        The timestamp represents the last time the host with the given
        ID contacted the hub. Returns None if the host has never contacted
         the hub."""
        query = """SELECT update_time FROM sessions
        JOIN host ON sessions.user_id = host.user_id
        WHERE host.id = %(hostID)i
        ORDER BY update_time DESC
        LIMIT 1
        """
        return _singleValue(query, locals(), strict=False)

    getAllArches = staticmethod(get_all_arches)

    getChannel = staticmethod(get_channel)
    listChannels=staticmethod(list_channels)

    getBuildroot=staticmethod(get_buildroot)

    def getBuildrootListing(self,id):
        """Return a list of packages in the buildroot"""
        br = BuildRoot(id)
        return br.getList()

    listBuildroots = staticmethod(query_buildroots)

    def hasPerm(self, perm):
        """Check if the logged-in user has the given permission.  Return False if
        they do not have the permission, or if they are not logged-in."""
        return context.session.hasPerm(perm)

    def getPerms(self):
        """Get a list of the permissions granted to the currently logged-in user."""
        return context.session.getPerms()

    def getUserPerms(self, userID):
        """Get a list of the permissions granted to the user with the given ID."""
        return koji.auth.get_user_perms(userID)

    def getAllPerms(self):
        """Get a list of all permissions in the system.  Returns a list of maps.  Each
        map contains the following keys:

        - id
        - name
        """
        query = """SELECT id, name FROM permissions
        ORDER BY id"""

        return _multiRow(query, {}, ['id', 'name'])

    def getAllChannels(self):
        """Get a list of all channels in the system.  Returns a list of maps.  Each
        map contains the following keys:

        - id
        - name
        """
        query = """SELECT id, name FROM channels
        ORDER BY id"""

        return _multiRow(query, {}, ['id', 'name'])

    def getLoggedInUser(self):
        """Return information about the currently logged-in user.  Returns data
        in the same format as getUser().  If there is no currently logged-in user,
        return None."""
        if context.session.logged_in:
            return self.getUser(context.session.user_id)
        else:
            return None

    def setBuildOwner(self, build, user):
        context.session.assertPerm('admin')
        buildinfo = get_build(build)
        if not buildinfo:
            raise koji.GenericError, 'build does not exist: %s' % build
        userinfo = get_user(user)
        if not userinfo:
            raise koji.GenericError, 'user does not exist: %s' % user
        userid = userinfo['id']
        buildid = buildinfo['id']
        q = """UPDATE build SET owner=%(userid)i WHERE id=%(buildid)i"""
        _dml(q,locals())

    def setBuildTimestamp(self, build, ts):
        """Set the completion time for a build

        build should a valid nvr or build id
        ts should be # of seconds since epoch or optionally an
            xmlrpc DateTime value"""
        context.session.assertPerm('admin')
        buildinfo = get_build(build)
        if not buildinfo:
            raise koji.GenericError, 'build does not exist: %s' % build
        elif isinstance(ts, xmlrpclib.DateTime):
            #not recommended
            #the xmlrpclib.DateTime class is almost useless
            try:
                ts = time.mktime(time.strptime(str(ts),'%Y%m%dT%H:%M:%S'))
            except ValueError:
                raise koji.GenericError, "Invalid time: %s" % ts
        elif not isinstance(ts, (int, long, float)):
            raise koji.GenericError, "Invalid type for timestamp"
        buildid = buildinfo['id']
        q = """UPDATE build
        SET completion_time=TIMESTAMP 'epoch' AT TIME ZONE 'utc' + '%(ts)f seconds'::interval
        WHERE id=%%(buildid)i""" % locals()
        _dml(q,locals())

    def count(self, methodName, *args, **kw):
        """Execute the XML-RPC method with the given name and count the results.
        A method return value of None will return O, a return value of type "list", "tuple", or "dict"
        will return len(value), and a return value of any other type will return 1.  An invalid
        methodName will raise an AttributeError, and invalid arguments will raise a TypeError."""
        result = getattr(self, methodName)(*args, **kw)
        if result == None:
            return 0
        elif isinstance(result, list) or isinstance(result, tuple) or isinstance(result, dict):
            return len(result)
        else:
            return 1

    def _sortByKeyFunc(self, key, noneGreatest=True):
        """Return a function to sort a list of maps by the given key.
        If the key starts with '-', sort in reverse order.  If noneGreatest
        is True, None will sort higher than all other values (instead of lower).
        """
        if noneGreatest:
            # Normally None evaluates to be less than every other value
            # Invert the comparison so it always evaluates to greater
            cmpFunc = lambda a, b: (a is None or b is None) and -(cmp(a, b)) or cmp(a, b)
        else:
            cmpFunc = cmp

        if key.startswith('-'):
            key = key[1:]
            return lambda a, b: cmpFunc(b[key], a[key])
        else:
            return lambda a, b: cmpFunc(a[key], b[key])

    def filterResults(self, methodName, *args, **kw):
        """Execute the XML-RPC method with the given name and filter the results
        based on the options specified in the keywork option "filterOpts".  The method
        must return a list of maps.  Any other return type will result in a TypeError.
        Currently supported options are:
        - offset: the number of elements to trim off the front of the list
        - limit: the maximum number of results to return
        - order: the map key to use to sort the list; the list will be sorted before
                 offset or limit are applied
        - noneGreatest: when sorting, consider 'None' to be greater than all other values;
                        python considers None less than all other values, but Postgres sorts
                        NULL higher than all other values; default to True for consistency
                        with database sorts
        """
        filterOpts = kw.pop('filterOpts', {})

        results = getattr(self, methodName)(*args, **kw)
        if results is None:
            return None
        elif not isinstance(results, list):
            raise TypeError, '%s() did not return a list' % methodName

        order = filterOpts.get('order')
        if order:
            results.sort(self._sortByKeyFunc(order, filterOpts.get('noneGreatest', True)))

        offset = filterOpts.get('offset')
        if offset is not None:
            results = results[offset:]
        limit = filterOpts.get('limit')
        if limit is not None:
            results = results[:limit]

        return results

    def getBuildNotifications(self, userID=None):
        """Get build notifications for the user with the given ID.  If no ID
        is specified, get the notifications for the currently logged-in user.  If
        there is no currently logged-in user, raise a GenericError."""
        if userID is None:
            user = self.getLoggedInUser()
            if user is None:
                raise koji.GenericError, 'not logged-in'
            else:
                userID = user['id']
        return get_build_notifications(userID)

    def getBuildNotification(self, id):
        """Get the build notification with the given ID.  Return None
        if there is no notification with the given ID."""
        fields = ('id', 'user_id', 'package_id', 'tag_id', 'success_only', 'email')
        query = """SELECT %s
        FROM build_notifications
        WHERE id = %%(id)i
        """ % ', '.join(fields)
        return _singleRow(query, locals(), fields)

    def updateNotification(self, id, package_id, tag_id, success_only):
        """Update an existing build notification with new data.  If the notification
        with the given ID doesn't exist, or the currently logged-in user is not the
        owner or the notification or an admin, raise a GenericError."""
        currentUser = self.getLoggedInUser()
        if not currentUser:
            raise koji.GenericError, 'not logged-in'

        orig_notif = self.getBuildNotification(id)
        if not orig_notif:
            raise koji.GenericError, 'no notification with ID: %i' % id
        elif not (orig_notif['user_id'] == currentUser['id'] or
                  self.hasPerm('admin')):
            raise koji.GenericError, 'user %i cannot update notifications for user %i' % \
                  (currentUser['id'], orig_notif['user_id'])

        update = """UPDATE build_notifications
        SET package_id = %(package_id)s,
        tag_id = %(tag_id)s,
        success_only = %(success_only)s
        WHERE id = %(id)i
        """

        _dml(update, locals())

    def createNotification(self, user_id, package_id, tag_id, success_only):
        """Create a new notification.  If the user_id does not match the currently logged-in user
        and the currently logged-in user is not an admin, raise a GenericError."""
        currentUser = self.getLoggedInUser()
        if not currentUser:
            raise koji.GenericError, 'not logged in'

        notificationUser = self.getUser(user_id)
        if not notificationUser:
            raise koji.GenericError, 'invalid user ID: %s' % user_id
        
        if not (notificationUser['id'] == currentUser['id'] or self.hasPerm('admin')):
            raise koji.GenericError, 'user %s cannot create notifications for user %s' % \
                  (currentUser['name'], notificationUser['name'])
        
        email = '%s@%s' % (notificationUser['name'], context.opts['EmailDomain'])
        insert = """INSERT INTO build_notifications
        (user_id, package_id, tag_id, success_only, email)
        VALUES
        (%(user_id)i, %(package_id)s, %(tag_id)s, %(success_only)s, %(email)s)
        """
        _dml(insert, locals())

    def deleteNotification(self, id):
        """Delete the notification with the given ID.  If the currently logged-in
        user is not the owner of the notification or an admin, raise a GenericError."""
        notification = self.getBuildNotification(id)
        if not notification:
            raise koji.GenericError, 'no notification with ID: %i' % id
        currentUser = self.getLoggedInUser()
        if not currentUser:
            raise koji.GenericError, 'not logged-in'

        if not (notification['user_id'] == currentUser['id'] or
                self.hasPerm('admin')):
            raise koji.GenericError, 'user %i cannot delete notifications for user %i' % \
                  (currentUser['id'], notification['user_id'])
        delete = """DELETE FROM build_notifications WHERE id = %(id)i"""
        _dml(delete, locals())

    def _prepareSearchTerms(self, terms, matchType):
        """Process the search terms before passing them to the database.
        If matchType is "glob", "_" will be replaced with "\_" (to match literal
        underscores), "?" will be replaced with "_", and "*" will
        be replaced with "%".  If matchType is "regexp", no changes will be
        made."""
        if matchType == 'glob':
            return terms.replace('\\', '\\\\').replace('_', r'\_').replace('?', '_').replace('*', '%')
        else:
            return terms

    _searchTables = {'package': 'package',
                     'build': 'build',
                     'tag': 'tag',
                     'target': 'build_target',
                     'user': 'users',
                     'host': 'host',
                     'rpm': 'rpminfo'}

    def search(self, terms, type, matchType, queryOpts=None):
        """Search for an item in the database matching "terms".
        "type" specifies what object type to search for, and must be
        one of "package", "build", "tag", "target", "user", "host",
        "rpm", or "file".  "matchType" specifies the type of search to
        perform, and must be one of "glob" or "regexp".  All searches
        are case-insensitive.  A list of maps containing "id" and
        "name" will be returned.  If no matches are found, an empty
        list will be returned."""
        if not terms:
            raise koji.GenericError, 'empty search terms'
        if type == 'file':
            # searching by filename is no longer supported
            return _applyQueryOpts([], queryOpts)
        table = self._searchTables.get(type)
        if not table:
            raise koji.GenericError, 'unknown search type: %s' % type

        if matchType == 'glob':
            oper = 'ilike'
        elif matchType == 'regexp':
            oper = '~*'
        else:
            oper = '='

        terms = self._prepareSearchTerms(terms, matchType)

        cols = ('id', 'name')
        aliases = cols
        joins = []
        if type == 'build':
            joins.append('package ON build.pkg_id = package.id')
            clause = "package.name || '-' || build.version || '-' || build.release %s %%(terms)s" % oper
            cols = ('build.id', "package.name || '-' || build.version || '-' || build.release")
        elif type == 'rpm':
            clause = "name || '-' || version || '-' || release || '.' || arch || '.rpm' %s %%(terms)s" % oper
            cols = ('id', "name || '-' || version || '-' || release || '.' || arch || '.rpm'")
        elif type == 'file':
            # only search for exact matches against files, so we can use the index and not thrash the disks
            clause = 'filename = %(terms)s'
            cols = ('rpm_id', 'filename')
        elif type == 'tag':
            joins.append('tag_config ON tag.id = tag_config.tag_id')
            clause = 'tag_config.active = TRUE and name %s %%(terms)s' % oper
        elif type == 'target':
            joins.append('build_target_config ON build_target.id = build_target_config.build_target_id')
            clause = 'build_target_config.active = TRUE and name %s %%(terms)s' % oper
        else:
            clause = 'name %s %%(terms)s' % oper

        query = QueryProcessor(columns=cols,
                               aliases=aliases, tables=(table,),
                               joins=joins, clauses=(clause,),
                               values=locals(), opts=queryOpts)
        return query.execute()


class BuildRoot(object):

    def __init__(self,id=None):
        if id is None:
            #db entry has yet to be created
            self.id = None
        else:
            logging.getLogger("koji.hub").debug("BuildRoot id: %s" % id)
            #load buildroot data
            self.load(id)

    def load(self,id):
        fields = ('id', 'host_id', 'repo_id', 'arch', 'task_id',
                    'create_event', 'retire_event', 'state')
        q = """SELECT %s FROM buildroot WHERE id=%%(id)i""" % (",".join(fields))
        data = _singleRow(q,locals(),fields,strict=False)
        if data == None:
            raise koji.GenericError, 'no buildroot with ID: %i' % id
        self.id = id
        self.data = data

    def new(self, host, repo, arch, task_id=None):
        state = koji.BR_STATES['INIT']
        id = _singleValue("SELECT nextval('buildroot_id_seq')", strict=True)
        q = """INSERT INTO buildroot(id,host_id,repo_id,arch,state,task_id)
        VALUES (%(id)i,%(host)i,%(repo)i,%(arch)s,%(state)i,%(task_id)s)"""
        _dml(q,locals())
        self.load(id)
        return self.id

    def verifyTask(self,task_id):
        if self.id is None:
            raise koji.GenericError, "buildroot not specified"
        return (task_id == self.data['task_id'])

    def assertTask(self,task_id):
        if not self.verifyTask(task_id):
            raise koji.ActionNotAllowed, 'Task %s does not have lock on buildroot %s' \
                                        %(task_id,self.id)

    def verifyHost(self,host_id):
        if self.id is None:
            raise koji.GenericError, "buildroot not specified"
        return (host_id == self.data['host_id'])

    def assertHost(self,host_id):
        if not self.verifyHost(host_id):
            raise koji.ActionNotAllowed, "Host %s not owner of buildroot %s" \
                                        % (host_id,self.id)

    def setState(self,state):
        if self.id is None:
            raise koji.GenericError, "buildroot not specified"
        id = self.id
        if isinstance(state,str):
            state = koji.BR_STATES[state]
        #sanity checks
        if state == koji.BR_STATES['INIT']:
            #we do not re-init buildroots
            raise koji.GenericError, "Cannot change buildroot state to INIT"
        q = """SELECT state,retire_event FROM buildroot WHERE id=%(id)s FOR UPDATE"""
        lstate,retire_event = _fetchSingle(q,locals(),strict=True)
        if koji.BR_STATES[lstate] == 'EXPIRED':
            #we will quietly ignore a request to expire an expired buildroot
            #otherwise this is an error
            if state == lstate:
                return
            else:
                raise koji.GenericError, "buildroot %i is EXPIRED" % id
        set = "state=%(state)s"
        if koji.BR_STATES[state] == 'EXPIRED':
            set += ",retire_event=get_event()"
        update = """UPDATE buildroot SET %s WHERE id=%%(id)s""" % set
        _dml(update,locals())
        self.data['state'] = state

    def getList(self):
        if self.id is None:
            raise koji.GenericError, "buildroot not specified"
        brootid = self.id
        fields = (
            ('rpm_id', 'rpm_id'),
            ('is_update', 'is_update'),
            ('rpminfo.name', 'name'),
            ('version', 'version'),
            ('release', 'release'),
            ('epoch', 'epoch'),
            ('arch', 'arch'),
            ('build_id', 'build_id'),
            ('external_repo_id', 'external_repo_id'),
            ('external_repo.name', 'external_repo_name'),
            )
        query = QueryProcessor(columns=[f[0] for f in fields], aliases=[f[1] for f in fields],
                        tables=['buildroot_listing'],
                        joins=["rpminfo ON rpm_id = rpminfo.id", "external_repo ON external_repo_id = external_repo.id"],
                        clauses=["buildroot_listing.buildroot_id = %(brootid)i"],
                        values=locals())
        return query.execute()

    def _setList(self,rpmlist,update=False):
        """Set or update the list of rpms in a buildroot"""
        if self.id is None:
            raise koji.GenericError, "buildroot not specified"
        brootid = self.id
        if update:
            current = dict([(r['rpm_id'],1) for r in self.getList()])
        q = """INSERT INTO buildroot_listing (buildroot_id,rpm_id,is_update)
        VALUES (%(brootid)s,%(rpm_id)s,%(update)s)"""
        rpm_ids = []
        for an_rpm in rpmlist:
            location = an_rpm.get('location')
            if location:
                data = add_external_rpm(an_rpm, location, strict=False)
                #will add if missing, compare if not
            else:
                data = get_rpm(an_rpm, strict=True)
            rpm_id = data['id']
            if update and current.has_key(rpm_id):
                #ignore duplicate packages for updates
                continue
            rpm_ids.append(rpm_id)
        #we sort to try to avoid deadlock issues
        rpm_ids.sort()
        for rpm_id in rpm_ids:
            _dml(q, locals())

    def setList(self,rpmlist):
        """Set the initial list of rpms in a buildroot"""
        if self.data['state'] != koji.BR_STATES['INIT']:
            raise koji.GenericError, "buildroot %(id)s in wrong state %(state)s" % self.data
        self._setList(rpmlist,update=False)

    def updateList(self,rpmlist):
        """Update the list of packages in a buildroot"""
        if self.data['state'] != koji.BR_STATES['BUILDING']:
            raise koji.GenericError, "buildroot %(id)s in wrong state %(state)s" % self.data
        self._setList(rpmlist,update=True)


class Host(object):

    def __init__(self,id=None):
        remote_id = context.session.getHostId()
        if id is None:
            id = remote_id
        if id is None:
            raise koji.AuthError, "No host specified"
        self.id = id
        self.same_host = (id == remote_id)

    def verify(self):
        """Verify that the remote host matches and has the lock"""
        if not self.same_host:
            raise koji.AuthError, "Host mismatch"
        if not context.session.exclusive:
            raise koji.AuthError, "This method requires an exclusive session"
        return True

    def taskUnwait(self,parent):
        """Clear wait data for task"""
        c = context.cnx.cursor()
        #unwait the task
        q = """UPDATE task SET waiting='false' WHERE id = %(parent)s"""
        context.commit_pending = True
        c.execute(q,locals())
        #...and un-await its subtasks
        q = """UPDATE task SET awaited='false' WHERE parent=%(parent)s"""
        c.execute(q,locals())

    def taskSetWait(self,parent,tasks):
        """Mark task waiting and subtasks awaited"""
        self.taskUnwait(parent)
        c = context.cnx.cursor()
        #mark tasks awaited
        q = """UPDATE task SET waiting='true' WHERE id=%(parent)s"""
        context.commit_pending = True
        c.execute(q,locals())
        if tasks is None:
            #wait on all subtasks
            q = """UPDATE task SET awaited='true' WHERE parent=%(parent)s"""
            c.execute(q,locals())
        else:
            for id in tasks:
                q = """UPDATE task SET awaited='true' WHERE id=%(id)s"""
                c.execute(q,locals())

    def taskWaitCheck(self,parent):
        """Return status of awaited subtask

        The return value is [finished, unfinished] where each entry
        is a list of task ids."""
        #check to see if any of the tasks have finished
        c = context.cnx.cursor()
        q = """
        SELECT id,state FROM task
        WHERE parent=%(parent)s AND awaited = TRUE"""
        c.execute(q,locals())
        canceled = koji.TASK_STATES['CANCELED']
        closed = koji.TASK_STATES['CLOSED']
        failed = koji.TASK_STATES['FAILED']
        finished = []
        unfinished = []
        for id,state in c.fetchall():
            if state in (canceled,closed,failed):
                finished.append(id)
            else:
                unfinished.append(id)
        return finished, unfinished

    def taskWait(self,parent):
        """Return task results or mark tasks as waited upon"""
        finished, unfinished = self.taskWaitCheck(parent)
        # un-await finished tasks
        if finished:
            context.commit_pending = True
            for id in finished:
                c = context.cnx.cursor()
                q = """UPDATE task SET awaited='false' WHERE id=%(id)s"""
                c.execute(q,locals())
        return [finished,unfinished]

    def taskWaitResults(self,parent,tasks):
        results = {}
        #if we're getting results, we're done waiting
        self.taskUnwait(parent)
        c = context.cnx.cursor()
        canceled = koji.TASK_STATES['CANCELED']
        closed = koji.TASK_STATES['CLOSED']
        failed = koji.TASK_STATES['FAILED']
        q = """
        SELECT id,state FROM task
        WHERE parent=%(parent)s"""
        if tasks is None:
            #query all subtasks
            tasks = []
            c.execute(q,locals())
            for id,state in c.fetchall():
                if state == canceled:
                    raise koji.GenericError, "Subtask canceled"
                elif state in (closed,failed):
                    tasks.append(id)
        #would use a dict, but xmlrpc requires the keys to be strings
        results = []
        for id in tasks:
            task = Task(id)
            results.append([id,task.getResult()])
        return results

    def getHostTasks(self):
        """get status of open tasks assigned to host"""
        c = context.cnx.cursor()
        host_id = self.id
        #query tasks
        fields = ['id','waiting','weight']
        st_open = koji.TASK_STATES['OPEN']
        q = """
        SELECT %s FROM task
        WHERE host_id = %%(host_id)s AND state = %%(st_open)s
        """  % (",".join(fields))
        c.execute(q,locals())
        tasks = [ dict(zip(fields,x)) for x in c.fetchall() ]
        for task in tasks:
            id = task['id']
            if task['waiting']:
                finished, unfinished = self.taskWaitCheck(id)
                if finished:
                    task['alert'] = True
        return tasks

    def updateHost(self,task_load,ready):
        host_data = get_host(self.id)
        if task_load != host_data['task_load'] or ready != host_data['ready']:
            c = context.cnx.cursor()
            id = self.id
            q = """UPDATE host SET task_load=%(task_load)s,ready=%(ready)s WHERE id=%(id)s"""
            c.execute(q,locals())
            context.commit_pending = True

    def getLoadData(self):
        """Get load balancing data

        This data is relatively small and the necessary load analysis is
        relatively complex, so we let the host machines crunch it."""
        return [get_ready_hosts(),get_active_tasks()]

    def getTask(self):
        """Open next available task and return it"""
        c = context.cnx.cursor()
        id = self.id
        #get arch and channel info for host
        q = """
        SELECT arches FROM host WHERE id = %(id)s
        """
        c.execute(q,locals())
        arches = c.fetchone()[0].split()
        q = """
        SELECT channel_id FROM host_channels WHERE host_id = %(id)s
        """
        c.execute(q,locals())
        channels = [ x[0] for x in c.fetchall() ]

        #query tasks
        fields = ['id', 'state', 'method', 'request', 'channel_id', 'arch', 'parent']
        st_free = koji.TASK_STATES['FREE']
        st_assigned = koji.TASK_STATES['ASSIGNED']
        q = """
        SELECT %s FROM task
        WHERE (state = %%(st_free)s)
            OR (state = %%(st_assigned)s AND host_id = %%(id)s)
        ORDER BY priority,create_time
        """  % (",".join(fields))
        c.execute(q,locals())
        for data in c.fetchall():
            data = dict(zip(fields,data))
            # XXX - we should do some pruning here, but for now...
            # check arch
            if data['arch'] not in arches:
                continue
            # NOTE: channels ignored for explicit assignments
            if data['state'] != st_assigned and data['channel_id'] not in channels:
                continue
            task = Task(data['id'])
            ret = task.open(self.id)
            if ret is None:
                #someone else got it while we were looking
                #log_error("task %s seems to be locked" % task['id'])
                continue
            return ret
        #else no appropriate tasks
        return None

    def isEnabled(self):
        """Return whether this host is enabled or not."""
        query = """SELECT enabled FROM host WHERE id = %(id)i"""
        return _singleValue(query, {'id': self.id}, strict=True)

class HostExports(object):
    '''Contains functions that are made available via XMLRPC'''

    def getID(self):
        host = Host()
        host.verify()
        return host.id

    def updateHost(self,task_load,ready):
        host = Host()
        host.verify()
        host.updateHost(task_load,ready)

    def getLoadData(self):
        host = Host()
        host.verify()
        return host.getLoadData()

    def getHost(self):
        """Return information about this host"""
        host = Host()
        host.verify()
        return get_host(host.id)

    def openTask(self,task_id):
        host = Host()
        host.verify()
        task = Task(task_id)
        return task.open(host.id)

    def getTask(self):
        host = Host()
        host.verify()
        return host.getTask()

    def closeTask(self,task_id,response):
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)
        return task.close(response)

    def failTask(self,task_id,response):
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)
        return task.fail(response)

    def freeTasks(self,tasks):
        host = Host()
        host.verify()
        for task_id in tasks:
            task = Task(task_id)
            if not task.verifyHost(host.id):
                #it's possible that a task was freed/reassigned since the host
                #last checked, so we should not raise an error
                continue
            task.free()
            #XXX - unfinished
            #remove any files related to task

    def setTaskWeight(self,task_id,weight):
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)
        return task.setWeight(weight)

    def getHostTasks(self):
        host = Host()
        host.verify()
        return host.getHostTasks()

    def taskSetWait(self,parent,tasks):
        host = Host()
        host.verify()
        return host.taskSetWait(parent,tasks)

    def taskWait(self,parent):
        host = Host()
        host.verify()
        return host.taskWait(parent)

    def taskWaitResults(self,parent,tasks):
        host = Host()
        host.verify()
        return host.taskWaitResults(parent,tasks)

    def subtask(self,method,arglist,parent,**opts):
        host = Host()
        host.verify()
        ptask = Task(parent)
        ptask.assertHost(host.id)
        opts['parent'] = parent
        if opts.has_key('label'):
            # first check for existing task with this parent/label
            q = """SELECT id FROM task
            WHERE parent=%(parent)s AND label=%(label)s"""
            row = _fetchSingle(q,opts)
            if row:
                #return task id
                return row[0]
        if opts.has_key('kwargs'):
            arglist = koji.encode_args(*arglist, **opts['kwargs'])
            del opts['kwargs']
        return make_task(method,arglist,**opts)

    def subtask2(self,__parent,__taskopts,__method,*args,**opts):
        """A wrapper around subtask with optional signature

        Parameters:
            __parent: task id of the parent task
            __taskopts: dictionary of task options
            __method: the method to be invoked

        Remaining args are passed on to the subtask
        """
        #self.subtask will verify the host
        args = koji.encode_args(*args,**opts)
        return self.subtask(__method,args,__parent,**__taskopts)

    def moveBuildToScratch(self, task_id, srpm, rpms, logs=None):
        "Move a completed scratch build into place (not imported)"
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)
        uploadpath = koji.pathinfo.work()
        #verify files exist
        for relpath in [srpm] + rpms:
            fn = "%s/%s" % (uploadpath,relpath)
            if not os.path.exists(fn):
                raise koji.GenericError, "no such file: %s" % fn

        rpms = check_noarch_rpms(uploadpath, rpms)

        #figure out storage location
        #  <scratchdir>/<username>/task_<id>
        scratchdir = koji.pathinfo.scratch()
        username = get_user(task.getOwner())['name']
        dir = "%s/%s/task_%s" % (scratchdir, username, task_id)
        koji.ensuredir(dir)
        for relpath in [srpm] + rpms:
            fn = "%s/%s" % (uploadpath,relpath)
            dest = "%s/%s" % (dir,os.path.basename(fn))
            os.rename(fn,dest)
            os.symlink(dest,fn)
        if logs:
            for key, files in logs.iteritems():
                if key:
                    logdir = "%s/logs/%s" % (dir, key)
                else:
                    logdir = "%s/logs" % dir
                koji.ensuredir(logdir)
                for relpath in files:
                    fn = "%s/%s" % (uploadpath,relpath)
                    dest = "%s/%s" % (logdir,os.path.basename(fn))
                    os.rename(fn,dest)
                    os.symlink(dest,fn)

    def initBuild(self,data):
        """Create a stub build entry.

        This is done at the very beginning of the build to inform the
        system the build is underway.
        """
        host = Host()
        host.verify()
        #sanity checks
        task = Task(data['task_id'])
        task.assertHost(host.id)
        #prep the data
        data['owner'] = task.getOwner()
        data['state'] = koji.BUILD_STATES['BUILDING']
        data['completion_time'] = None
        return new_build(data)

    def completeBuild(self, task_id, build_id, srpm, rpms, brmap=None, logs=None):
        """Import final build contents into the database"""
        #sanity checks
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)
        result = import_build(srpm, rpms, brmap, task_id, build_id, logs=logs)
        build_notification(task_id, build_id)
        return result

    def failBuild(self, task_id, build_id):
        """Mark the build as failed.  If the current state is not
        'BUILDING', or the current competion_time is not null, a
        GenericError will be raised."""
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)

        query = """SELECT state, completion_time
        FROM build
        WHERE id = %(build_id)i
        FOR UPDATE"""
        result = _singleRow(query, locals(), ('state', 'completion_time'))

        if not result:
            raise koji.GenericError, 'no build with ID: %i' % build_id
        elif result['state'] != koji.BUILD_STATES['BUILDING']:
            raise koji.GenericError, 'cannot update build %i, state: %s' % \
                  (build_id, koji.BUILD_STATES[result['state']])
        elif result['completion_time'] is not None:
            raise koji.GenericError, 'cannot update build %i, completed at %s' % \
                  (build_id, result['completion_time'])

        state = koji.BUILD_STATES['FAILED']
        update = """UPDATE build
        SET state = %(state)i,
        completion_time = NOW()
        WHERE id = %(build_id)i"""
        _dml(update, locals())
        build_notification(task_id, build_id)

    def tagBuild(self,task_id,tag,build,force=False,fromtag=None):
        """Tag a build (host version)

        This tags as the user who owns the task

        If fromtag is specified, also untag the package (i.e. move in a single
        transaction)

        No return value
        """
        host = Host()
        host.verify()
        task = Task(task_id)
        task.assertHost(host.id)
        user_id = task.getOwner()
        policy_data = {'tag' : tag, 'build' : build, 'fromtag' : fromtag}
        policy_data['user_id'] = user_id
        if fromtag is None:
            policy_data['operation'] = 'tag'
        else:
            policy_data['operation'] = 'move'
        #don't check policy for admins using force
        perms = koji.auth.get_user_perms(user_id)
        if not force or 'admin' not in perms:
            assert_policy('tag', policy_data)
        if fromtag:
            _untag_build(fromtag,build,user_id=user_id,force=force,strict=True)
        _tag_build(tag,build,user_id=user_id,force=force)

    # Called from kojid::LiveCDTask
    def importImage(self, task_id, filename, filesize, arch, mediatype, hash, rpmlist):
        image_id = importImageInternal(task_id, filename, filesize, arch, mediatype,
                                       hash, rpmlist)
        moveImageResults(task_id, image_id, arch)
        return image_id

    def tagNotification(self, is_successful, tag_id, from_id, build_id, user_id, ignore_success=False, failure_msg=''):
        """Create a tag notification message.
        Handles creation of tagNotification tasks for hosts."""
        host = Host()
        host.verify()
        tag_notification(is_successful, tag_id, from_id, build_id, user_id, ignore_success, failure_msg)

    def checkPolicy(self, name, data, default='deny', strict=False):
        host = Host()
        host.verify()
        return check_policy(name, data, default=default, strict=strict)

    def assertPolicy(self, name, data, default='deny'):
        host = Host()
        host.verify()
        check_policy(name, data, default=default, strict=True)

    def newBuildRoot(self, repo, arch, task_id=None):
        host = Host()
        host.verify()
        if task_id is not None:
            Task(task_id).assertHost(host.id)
        br = BuildRoot()
        return br.new(host.id,repo,arch,task_id=task_id)

    def setBuildRootState(self,brootid,state,task_id=None):
        host = Host()
        host.verify()
        if task_id is not None:
            Task(task_id).assertHost(host.id)
        br = BuildRoot(brootid)
        br.assertHost(host.id)
        if task_id is not None:
            br.assertTask(task_id)
        return br.setState(state)

    def setBuildRootList(self,brootid,rpmlist,task_id=None):
        host = Host()
        host.verify()
        if task_id is not None:
            Task(task_id).assertHost(host.id)
        br = BuildRoot(brootid)
        br.assertHost(host.id)
        if task_id is not None:
            br.assertTask(task_id)
        return br.setList(rpmlist)

    def updateBuildRootList(self,brootid,rpmlist,task_id=None):
        host = Host()
        host.verify()
        if task_id is not None:
            Task(task_id).assertHost(host.id)
        br = BuildRoot(brootid)
        br.assertHost(host.id)
        if task_id is not None:
            br.assertTask(task_id)
        return br.updateList(rpmlist)

    def repoInit(self, tag, with_src=False, with_debuginfo=False, event=None):
        """Initialize a new repo for tag"""
        host = Host()
        host.verify()
        return repo_init(tag, with_src=with_src, with_debuginfo=with_debuginfo, event=event)

    def repoAddRPM(self, repo_id, path):
        """Add an uploaded rpm to a repo"""
        host = Host()
        host.verify()
        rinfo = repo_info(repo_id, strict=True)
        repodir = koji.pathinfo.repo(repo_id, rinfo['tag_name'])
        if rinfo['state'] != koji.REPO_INIT:
            raise koji.GenericError, "Repo %(id)s not in INIT state (got %(state)s)" % rinfo
        #verify file exists
        uploadpath = koji.pathinfo.work()
        filepath = "%s/%s" % (uploadpath, path)
        if not os.path.exists(filepath):
            raise koji.GenericError, "no such file: %s" % filepath
        rpminfo = koji.get_header_fields(filepath, ('arch','sourcepackage'))
        dirs = []
        if not rpminfo['sourcepackage'] and rpminfo['arch'] != 'noarch':
            arch = koji.canonArch(rpminfo['arch'])
            dir = "%s/%s/RPMS" % (repodir, arch)
            if os.path.isdir(dir):
                dirs.append(dir)
        else:
            #noarch and srpms linked for all arches
            for fn in os.listdir(repodir):
                if fn == 'groups':
                    continue
                if rpminfo['sourcepackage']:
                    dir = "%s/%s/SRPMS" % (repodir, fn)
                else:
                    dir = "%s/%s/RPMS" % (repodir, fn)
                if os.path.isdir(dir):
                    dirs.append(dir)
        for dir in dirs:
            fn = os.path.basename(filepath)
            dst = "%s/%s" % (dir, fn)
            if os.path.exists(dst):
                s_st = os.stat(filepath)
                d_st = os.stat(dst)
                if s_st.st_ino != d_st.st_ino:
                    raise koji.GenericError, "File already in repo: %s" % dst
                #otherwise the desired hardlink already exists
            else:
                os.link(filepath, dst)

    def repoDone(self, repo_id, data, expire=False):
        """Move repo data into place, mark as ready, and expire earlier repos

        repo_id: the id of the repo
        data: a dictionary of the form { arch: (uploadpath, files), ...}
        expire(optional): if set to true, mark the repo expired immediately*

        * This is used when a repo from an older event is generated
        """
        host = Host()
        host.verify()
        rinfo = repo_info(repo_id, strict=True)
        if rinfo['state'] != koji.REPO_INIT:
            raise koji.GenericError, "Repo %(id)s not in INIT state (got %(state)s)" % rinfo
        repodir = koji.pathinfo.repo(repo_id, rinfo['tag_name'])
        workdir = koji.pathinfo.work()
        for arch, (uploadpath, files) in data.iteritems():
            archdir = "%s/%s" % (repodir, arch)
            if not os.path.isdir(archdir):
                raise koji.GenericError, "Repo arch directory missing: %s" % archdir
            datadir = "%s/repodata" % archdir
            koji.ensuredir(datadir)
            for fn in files:
                src = "%s/%s/%s" % (workdir,uploadpath, fn)
                dst = "%s/%s" % (datadir, fn)
                if not os.path.exists(src):
                    raise koji.GenericError, "uploaded file missing: %s" % src
                os.link(src, dst)
                os.unlink(src)
        if expire:
            repo_expire(repo_id)
            return
        #else:
        repo_ready(repo_id)
        repo_expire_older(rinfo['tag_id'], rinfo['create_event'])
        #make a latest link
        latestrepolink = koji.pathinfo.repo('latest', rinfo['tag_name'])
        #XXX - this is a slight abuse of pathinfo
        try:
            if os.path.lexists(latestrepolink):
                os.unlink(latestrepolink)
            os.symlink(str(repo_id), latestrepolink)
        except OSError:
            #making this link is nonessential
            log_error("Unable to create latest link for repo: %s" % repodir)


    def isEnabled(self):
        host = Host()
        host.verify()
        return host.isEnabled()

# XXX - not needed anymore?
def handle_upload(req):
    """Handle file upload via POST request"""
    pass

#koji.add_sys_logger("koji")

if __name__ == "__main__":
    # XXX - testing defaults
    print "Connecting to DB"
    koji.db.setDBopts( database = "test", user = "test")
    context.cnx = koji.db.connect()
    context.req = {}
    print "Creating a session"
    context.session = koji.auth.Session(None,hostip="127.0.0.1")
    print context.session
    test_user = "host/1"
    pw = "foobar"
    print "Logging in as %s" % test_user
    session_info = context.session.login(test_user,pw,{'hostip':'127.0.0.1'})
    for k in session_info.keys():
        session_info[k] = [session_info[k]]
    s2=koji.auth.Session(session_info,'127.0.0.1')
    print s2
    print s2.getHostId()
    context.session = s2
    print "Associating host"
    Host()
    #context.cnx.commit()
    context.session.perms['admin'] = 1 #XXX
