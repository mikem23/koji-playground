# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Copyright 2005 Dan Williams <dcbw@redhat.com> and Red Hat, Inc.

import os, sys
from OpenSSL import SSL
import SSLConnection
import httplib
import socket
import SocketServer

def our_verify(connection, x509, errNum, errDepth, preverifyOK):
    # print "Verify: errNum = %s, errDepth = %s, preverifyOK = %s" % (errNum, errDepth, preverifyOK)

    # preverifyOK should tell us whether or not the client's certificate
    # correctly authenticates against the CA chain
    return preverifyOK


def CreateSSLContext(certs):
    key_and_cert = certs['key_and_cert']
    peer_ca_cert = certs['peer_ca_cert']
    for f in key_and_cert, peer_ca_cert:
        if f and not os.access(f, os.R_OK):
            raise StandardError, "%s does not exist or is not readable" % f

    ctx = SSL.Context(SSL.SSLv23_METHOD)   # Use best possible TLS Method
    ctx.use_certificate_file(key_and_cert)
    ctx.use_privatekey_file(key_and_cert)
    ctx.load_verify_locations(peer_ca_cert)
    verify = SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT
    ctx.set_verify(verify, our_verify)
    ctx.set_verify_depth(10)
    ctx.set_options(SSL.OP_NO_SSLv3 | SSL.OP_NO_SSLv2) # disable SSLv2 and SSLv3
    return ctx


class PlgHTTPSConnection(httplib.HTTPConnection):
    "This class allows communication via SSL."

    response_class = httplib.HTTPResponse

    def __init__(self, host, port=None, ssl_context=None, strict=None, timeout=None):
        httplib.HTTPConnection.__init__(self, host, port, strict)
        self.ssl_ctx = ssl_context
        self._timeout = timeout

    def connect(self):
        for res in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            try:
                sock = socket.socket(af, socktype, proto)
                con = SSL.Connection(self.ssl_ctx, sock)
                self.sock = SSLConnection.SSLConnection(con)
                if sys.version_info[:3] >= (2, 3, 0):
                    self.sock.settimeout(self._timeout)
                self.sock.connect(sa)
                if self.debuglevel > 0:
                    print "connect: (%s, %s) [ssl]" % (self.host, self.port)
            except socket.error, msg:
                if self.debuglevel > 0:
                    print 'connect fail:', (self.host, self.port)
                if self.sock:
                    self.sock.close()
                self.sock = None
                continue
            break
        else:
            raise socket.error, "failed to connect"
