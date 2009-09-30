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

import calendar
import re
import time
import koji
from zlib import adler32

try:
    from hashlib import md5 as md5_constructor
except ImportError:
    from md5 import new as md5_constructor

def _changelogDate(cldate):
    return time.strftime('%a %b %d %Y', time.strptime(koji.formatTime(cldate), '%Y-%m-%d %H:%M:%S'))

def formatChangelog(entries):
    """Format a list of changelog entries (dicts)
    into a string representation."""
    result = ''
    for entry in entries:
        result += """* %s %s
%s

""" % (_changelogDate(entry['date']), entry['author'], entry['text'])

    return result

DATE_RE = re.compile(r'(\d+)-(\d+)-(\d+)')
TIME_RE = re.compile(r'(\d+):(\d+):(\d+)')

def parseTime(val):
    """
    Parse a string time in either "YYYY-MM-DD HH24:MI:SS" or "YYYY-MM-DD"
    format into floating-point seconds since the epoch.  If the time portion
    is not specified, it will be padded with zeros.  The string time is treated
    as UTC.  If the time string cannot be parsed into a valid date, None will be
    returned.
    """
    result = DATE_RE.search(val)
    if not result:
        return None
    else:
        date = [int(r) for r in result.groups()]
    time = [0, 0, 0]
    rest = val[result.end():].strip()
    result = TIME_RE.search(rest)
    if result:
        time = [int(r) for r in result.groups()]
    return calendar.timegm(date + time + [0, 0, 0])

def checkForBuilds(session, tag, builds, event):
    """Check that the builds existed in tag at the time of the event."""
    for build in builds:
        tagged_list = session.listTagged(tag, event=event, package=build['name'], inherit=True)
        for tagged in tagged_list:
            if tagged['version'] == build['version'] and tagged['release'] == build['release']:
                break
        else:
            return False

    return True

def duration(start):
    """Return the duration between start and now in MM:SS format"""
    elapsed = time.time() - start
    mins = int(elapsed / 60)
    secs = int(elapsed % 60)
    return '%s:%02i' % (mins, secs)

def printList(l):
    """Print the contents of the list comma-separated"""
    if len(l) == 0:
        return ''
    elif len(l) == 1:
        return l[0]
    elif len(l) == 2:
        return ' and '.join(l)
    else:
        ret = ', '.join(l[:-1])
        ret += ', and '
        ret += l[-1]
        return ret

def eventFromOpts(session, opts):
    """Determine event id from standard cli options

    Standard options are:
        event: an event id (int)
        ts: an event timestamp (int)
        repo: pull event from given repo
    """
    event_id = getattr(opts, 'event')
    if event_id:
        return session.getEvent(event_id)
    ts = getattr(opts, 'ts')
    if ts:
        return session.getLastEvent(before=ts)
    repo = getattr(opts, 'repo')
    if repo:
        rinfo = session.repoInfo(repo)
        if rinfo:
            return {'id' : rinfo['create_event'],
                    'ts' : rinfo['create_ts'] }
    return None

def filedigestAlgo(hdr):
    """
    Get the file digest algorithm used in hdr.
    If there is no algorithm flag in the header,
    default to md5.  If the flag contains an unknown,
    non-None value, return 'unknown'.
    """
    # need to use the header ID hard-coded into Koji so we're not dependent on the
    # version of rpm installed on the hub
    digest_algo_id = hdr[koji.RPM_TAG_FILEDIGESTALGO]
    if not digest_algo_id:
        # certain versions of rpm return an empty list instead of None
        # for missing header fields
        digest_algo_id = None
    digest_algo = koji.RPM_FILEDIGESTALGO_IDS.get(digest_algo_id, 'unknown')
    return digest_algo.lower()


class adler32_constructor(object):

    #mimicing the hashlib constructors
    def __init__(self, arg=''):
        self._value = adler32(arg)

    def update(self, arg):
        self._value = adler32(arg, self._value)

    def digest(self):
        return self._value

    def hexdigest(self):
        return "%08x" % self._value

    def copy(self):
        dup = adler32_constructor()
        dup._value = self._value
        return dup

    digest_size = 4
    block_size = 1      #I think

