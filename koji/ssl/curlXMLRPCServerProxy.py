#!/usr/bin/python -tt
#
# PyCURL based transports for xmlrpclib
#
# Copyright 2009 Red Hat inc

# Based on a implementation by Alexandr D. Kanevskiy <kad () bifh.org>
# which was based on pycurl's example by Kjetil Jacobsen <kjetilja at gmail.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA
# 02110-1301 USA
#

from StringIO import StringIO
import xmlrpclib, pycurl

class PyCURLXMLRPCTransport(xmlrpclib.Transport):
    """Handles a cURL HTTPS transaction to an XML-RPC server validating certs."""
    def __init__(self, certs, timeout=None):
        xmlrpclib.Transport.__init__(self)

        self.verbose = 0
        self._proto = "https"

        self._curl = pycurl.Curl()

        # Suppress signals
        self._curl.setopt(pycurl.NOSIGNAL, 1)

        # Follow redirects
        self._curl.setopt(pycurl.FOLLOWLOCATION, 1)

        # Set timeouts
        if timeout:
            self._curl.setopt(pycurl.CONNECTTIMEOUT, timeout)
            self._curl.setopt(pycurl.TIMEOUT, timeout)

        # XML-RPC calls are POST (text/xml)
        self._curl.setopt(pycurl.POST, 1)
        self._curl.setopt(pycurl.HTTPHEADER, [ "Content-Type: text/xml" ])
        #certs['ca_cert']
        #certs['peer_ca_cert']
        # Setup certificates
        self._curl.setopt(pycurl.CAINFO, certs['ca_cert'])
        self._curl.setopt(pycurl.SSLCERT, certs['key_and_cert'])
        self._curl.setopt(pycurl.SSLKEY, certs['key_and_cert'])
        self._curl.setopt(pycurl.SSL_VERIFYPEER, 2)
        self._curl.setopt(pycurl.SSL_VERIFYHOST, 2)

    def _check_return(self, host, handler, httpcode, buf):
        """Check for SSL certs validity"""
        if httpcode == 60:
            raise xmlrpclib.ProtocolError(
                    host + handler,
                    httpcode,
                    "SSL certificate validation failed",
                    None)

    def request(self, host, handler, request_body, verbose = 0):
        """Performs actual request"""
        buf = StringIO()
        self._curl.setopt(pycurl.URL,
                "%s://%s%s" % (self._proto, host, handler))
        self._curl.setopt(pycurl.POSTFIELDS, request_body)
        self._curl.setopt(pycurl.WRITEFUNCTION, buf.write)
        self._curl.setopt(pycurl.VERBOSE, verbose)
        self.verbose = verbose
        try:
            self._curl.perform()
            httpcode = self._curl.getinfo(pycurl.HTTP_CODE)
        except pycurl.error, err:
            raise xmlrpclib.ProtocolError(
                    host + handler,
                    err[0],
                    err[1],
                    None)

        self._check_return(host, handler, httpcode, buf)

        if httpcode != 200:
            raise xmlrpclib.ProtocolError(
                    host + handler,
                    httpcode,
                    buf.getvalue(),
                    None)

        buf.seek(0)
        return self.parse_response(buf)

class KojiXMLRPCServerProxy(xmlrpclib.ServerProxy):
    def __init__(self, uri, certs, timeout=None, verbose=0, allow_none=0):
        self._transport = PyCURLXMLRPCTransport(certs, timeout=timeout)
        xmlrpclib.ServerProxy.__init__(self, uri, transport=self._transport,
                                       verbose=verbose, allow_none=allow_none)

    def cancel(self):
        self._transport.close()
