%{!?python_sitelib: %define python_sitelib %(%{__python} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}

%define baserelease 1
#build with --define 'testbuild 1' to have a timestamp appended to release
%if "x%{?testbuild}" == "x1"
%define release %{baserelease}.%(date +%%Y%%m%%d.%%H%%M.%%S)
%else
%define release %{baserelease}
%endif
Name: koji
Version: 1.3.1
Release: %{release}%{?dist}
License: LGPLv2 and GPLv2+
# koji.ssl libs (from plague) are GPLv2+
Summary: Build system tools
Group: Applications/System
URL: http://fedorahosted.org/koji
Source: https://fedorahosted.org/koji/attachment/wiki/KojiRelease/%{name}-%{PACKAGE_VERSION}.tar.bz2
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
BuildArch: noarch
Requires: python-krbV >= 1.0.13
Requires: rpm-python
Requires: pyOpenSSL
Requires: python-urlgrabber
BuildRequires: python

%description
Koji is a system for building and tracking RPMS.  The base package
contains shared libraries and the command-line interface.

%package hub
Summary: Koji XMLRPC interface
Group: Applications/Internet
License: LGPLv2 and GPLv2
# rpmdiff lib (from rpmlint) is GPLv2 (only)
Requires: httpd
Requires: mod_python
Requires: postgresql-python
Requires: %{name} = %{version}-%{release}

%description hub
koji-hub is the XMLRPC interface to the koji database

%package builder
Summary: Koji RPM builder daemon
Group: Applications/System
License: LGPLv2 and GPLv2+
#mergerepos (from createrepo) is GPLv2+
Requires: %{name} = %{version}-%{release}
Requires: mock >= 0.9.14
Requires(post): /sbin/chkconfig
Requires(post): /sbin/service
Requires(preun): /sbin/chkconfig
Requires(preun): /sbin/service
Requires(pre): /usr/sbin/useradd
Requires: /usr/bin/cvs
Requires: /usr/bin/svn
Requires: /usr/bin/git
Requires: rpm-build
Requires: redhat-rpm-config
Requires: pykickstart
Requires: pycdio
%if 0%{?rhel} >= 5
Requires: createrepo >= 0.4.11-2
%endif
%if 0%{?fedora} >= 9
Requires: createrepo >= 0.9.2
%endif

%description builder
koji-builder is the daemon that runs on build machines and executes
tasks that come through the Koji system.

%package utils
Summary: Koji Utilities
Group: Applications/Internet
License: LGPLv2
Requires: postgresql-python
Requires: %{name} = %{version}-%{release}

%description utils
Utilities for the Koji system

%package web
Summary: Koji Web UI
Group: Applications/Internet
License: LGPLv2
Requires: httpd
Requires: mod_python
Requires: mod_auth_kerb
Requires: postgresql-python
Requires: python-cheetah
Requires: %{name} = %{version}-%{release}
Requires: python-krbV >= 1.0.13

%description web
koji-web is a web UI to the Koji system.

%prep
%setup -q

%build

%install
rm -rf $RPM_BUILD_ROOT
make DESTDIR=$RPM_BUILD_ROOT install

%clean
rm -rf $RPM_BUILD_ROOT

%files
%defattr(-,root,root)
%{_bindir}/*
%{python_sitelib}/%{name}
%config(noreplace) %{_sysconfdir}/koji.conf
%doc docs Authors COPYING LGPL

%files hub
%defattr(-,root,root)
%{_datadir}/koji-hub
%dir %{_libexecdir}/koji-hub
%{_libexecdir}/koji-hub/rpmdiff
%config(noreplace) /etc/httpd/conf.d/kojihub.conf
%config(noreplace) /etc/koji-hub/hub.conf

%files utils
%defattr(-,root,root)
%{_sbindir}/kojira
%{_initrddir}/kojira
%config(noreplace) %{_sysconfdir}/sysconfig/kojira
%dir %{_sysconfdir}/kojira
%config(noreplace) %{_sysconfdir}/kojira/kojira.conf
%{_sbindir}/koji-gc
%dir %{_sysconfdir}/koji-gc
%config(noreplace) %{_sysconfdir}/koji-gc/koji-gc.conf
%{_sbindir}/koji-shadow
%dir %{_sysconfdir}/koji-shadow
%config(noreplace) %{_sysconfdir}/koji-shadow/koji-shadow.conf

%files web
%defattr(-,root,root)
%{_datadir}/koji-web
%{_sysconfdir}/kojiweb
%config(noreplace) /etc/httpd/conf.d/kojiweb.conf

%files builder
%defattr(-,root,root)
%{_sbindir}/kojid
%dir %{_libexecdir}/kojid
%{_libexecdir}/kojid/mergerepos
%{_initrddir}/kojid
%config(noreplace) %{_sysconfdir}/sysconfig/kojid
%dir %{_sysconfdir}/kojid
%config(noreplace) %{_sysconfdir}/kojid/kojid.conf
%{_datadir}/koji-builder
%attr(-,kojibuilder,kojibuilder) /etc/mock/koji

%pre builder
/usr/sbin/useradd -r -s /bin/bash -G mock -d /builddir -M kojibuilder 2>/dev/null ||:

%post builder
/sbin/chkconfig --add kojid
/sbin/service kojid condrestart &> /dev/null || :

%preun builder
if [ $1 = 0 ]; then
  /sbin/service kojid stop &> /dev/null
  /sbin/chkconfig --del kojid
fi

%post utils
/sbin/chkconfig --add kojira
/sbin/service kojira condrestart &> /dev/null || :
%preun utils
if [ $1 = 0 ]; then
  /sbin/service kojira stop &> /dev/null || :
  /sbin/chkconfig --del kojira
fi

%changelog
* Fri Feb 20 2009 Mike McLean <mikem at redhat.com> 1.3.1-1
- external repo urls rewritten to end with /
- add schema file for upgrades from 1.2.x to 1.3
- explicitly request sha1 for backward compatibility with older yum
- fix up sparc arch handling

* Wed Feb 18 2009 Mike McLean <mikem at redhat.com> 1.3.0-1
- support for external repos
- support for noarch subpackages
- support rpms with different signatures and file digests
- hub configuration file
- drop huge tables from database
- build srpms in chroots
- hub policies
- limited plugin support
- limited web ui theming
- many miscellaneous enhancements and bugfixes
- license fields changed to reflect code additions

* Mon Aug 25 2008 Mike McLean <mikem@redhat.com> 1.2.6-1
- fix testbuild conditional [downstream]
- fix license tag [downstream]
- bump version
- more robust client sessions
- handle errors gracefully in web ui
- koji-gc added to utils subpackage
- skip sleep in kojid after taking a task
- new dir layout for task workdirs (avoids large directories)
- unified boolean option parsing in kojihub
- new ServerOffline exception
- other miscellaneous fixes

* Fri Jan 25 2008 jkeating <jkeating@redhat.com> 1.2.5-1
- Put createrepo arguments in correct order

* Thu Jan 24 2008 jkeating <jkeating@redhat.com> 1.2.4-1
- Use the --skip-stat flag in createrepo calls.
- canonicalize tag arches before using them (dgilmore)
- fix return value of delete_build
- Revert to getfile urls if the task is not successful in emails
- Pass --target instead of --arch to mock.
- ignore trashcan tag in prune-signed-copies command
- add the "allowed_scms" kojid parameter
- allow filtering builds by the person who built them

* Fri Dec 14 2007 jkeating <jkeating@redhat.com> 1.2.3-1
- New upstream release with lots of updates, bugfixes, and enhancements.

* Tue Jun  5 2007 Mike Bonnet <mikeb@redhat.com> - 1.2.2-1
- only allow admins to perform non-scratch builds from srpm
- bug fixes to the cmd-line and web UIs

* Thu May 31 2007 Mike Bonnet <mikeb@redhat.com> - 1.2.1-1
- don't allow ExclusiveArch to expand the archlist (bz#239359)
- add a summary line stating whether the task succeeded or failed to the end of the "watch-task" output
- add a search box to the header of every page in the web UI
- new koji download-build command (patch provided by Dan Berrange)

* Tue May 15 2007 Mike Bonnet <mikeb@redhat.com> - 1.2.0-1
- change version numbering to a 3-token scheme
- install the koji favicon

* Mon May 14 2007 Mike Bonnet <mikeb@redhat.com> - 1.1-5
- cleanup koji-utils Requires
- fix encoding and formatting in email notifications
- expand archlist based on ExclusiveArch/BuildArchs
- allow import of rpms without srpms
- commit before linking in prepRepo to release db locks
- remove exec bit from kojid logs and uploaded files (patch by Enrico Scholz)

* Tue May  1 2007 Mike Bonnet <mikeb@redhat.com> - 1.1-4
- remove spurious Requires: from the koji-utils package

* Tue May  1 2007 Mike Bonnet <mikeb@redhat.com> - 1.1-3
- fix typo in BuildNotificationTask (patch provided by Michael Schwendt)
- add the --changelog param to the buildinfo command
- always send email notifications to the package builder and package owner
- improvements to the web UI

* Tue Apr 17 2007 Mike Bonnet <mikeb@redhat.com> - 1.1-2
- re-enable use of the --update flag to createrepo

* Mon Apr 09 2007 Jesse Keating <jkeating@redhat.com> 1.1-1
- make the output listPackages() consistent regardless of with_dups
- prevent large batches of repo deletes from holding up regens
- allow sorting the host list by arches

* Mon Apr 02 2007 Jesse Keating <jkeating@redhat.com> 1.0-1
- Release 1.0!

* Wed Mar 28 2007 Mike Bonnet <mikeb@redhat.com> - 0.9.7-4
- set SSL connection timeout to 12 hours

* Wed Mar 28 2007 Mike Bonnet <mikeb@redhat.com> - 0.9.7-3
- avoid SSL renegotiation
- improve log file handling in kojid
- bug fixes in command-line and web UI

* Sun Mar 25 2007 Mike Bonnet <mikeb@redhat.com> - 0.9.7-2
- enable http access to packages in kojid
- add Requires: pyOpenSSL
- building srpms from CVS now works with the Extras CVS structure
- fixes to the chain-build command
- bug fixes in the XML-RPC and web interfaces

* Tue Mar 20 2007 Jesse Keating <jkeating@redhat.com> - 0.9.7-1
- Package up the needed ssl files

* Tue Mar 20 2007 Jesse Keating <jkeating@redhat.com> - 0.9.6-1
- 0.9.6 release, mostly ssl auth stuff
- use named directories for config stuff
- remove -3 requires on creatrepo, don't need that specific anymore

* Tue Feb 20 2007 Jesse Keating <jkeating@redhat.com> - 0.9.5-8
- Add Authors COPYING LGPL to the docs of the main package

* Tue Feb 20 2007 Jesse Keating <jkeating@redhat.com> - 0.9.5-7
- Move web files from /var/www to /usr/share
- Use -p in install calls
- Add rpm-python to requires for koji

* Mon Feb 19 2007 Jesse Keating <jkeating@redhat.com> - 0.9.5-6
- Clean up spec for package review

* Sun Feb 04 2007 Mike McLean <mikem@redhat.com> - 0.9.5-1
- project renamed to koji
