#koji hub plugin
# There is a kojid plugin that goes with this hub plugin. The kojid builder
# plugin has a config file.  This hub plugin has no config file.


from __future__ import absolute_import
from koji.context import context
from koji.plugin import export
import koji
import random
import six
import sys

#XXX - have to import kojihub for make_task
if six.PY3:
    sys.path.insert(0, '/usr/share/koji-hub-python3/')
else:
    sys.path.insert(0, '/usr/share/koji-hub/')
sys.path.insert(0, '/usr/share/koji-hub/')
import kojihub

__all__ = ('runroot',)


def get_channel_arches(channel):
    """determine arches available in channel"""
    chan = context.handlers.call('getChannel', channel, strict=True)
    ret = {}
    for host in context.handlers.call('listHosts', channelID=chan['id'], enabled=True):
        for a in host['arches'].split():
            ret[koji.canonArch(a)] = 1
    return ret

@export
def runroot(tagInfo, arch, command, channel=None, **opts):
    """ Create a runroot task """
    context.session.assertPerm('runroot')
    taskopts = {
        'priority': 15,
        'arch': arch,
    }

    taskopts['channel'] = channel or 'runroot'

    if arch == 'noarch':
        #not all arches can generate a proper buildroot for all tags
        tag = kojihub.get_tag(tagInfo)
        if not tag['arches']:
            raise koji.GenericError('no arches defined for tag %s' % tag['name'])

        #get all known arches for the system
        fullarches = kojihub.get_all_arches()

        tagarches = tag['arches'].split()

        # If our tag can't do all arches, then we need to
        # specify one of the arches it can do.
        if set(fullarches) - set(tagarches):
            chanarches = get_channel_arches(taskopts['channel'])
            choices = [x for x in tagarches if x in chanarches]
            if not choices:
                raise koji.GenericError('no common arches for tag/channel: %s/%s' \
                            % (tagInfo, taskopts['channel']))
            taskopts['arch'] = koji.canonArch(random.choice(choices))

    args = koji.encode_args(tagInfo, arch, command, **opts)
    return kojihub.make_task('runroot', args, **taskopts)

