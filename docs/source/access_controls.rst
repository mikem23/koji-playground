===============
Access Controls
===============

Koji is a complex system, so there are many places where some kind of access
control is used. Here is the documentation hub for all the mechanisms in place.

User/Builder Authentication
===========================

Users (and builders) are authenticated via one of the following mechanisms. Most
preferred is GSSAPI/Kerberos authentication. Second best is authentication via
SSL certificates. Mostly for testing environments we also support authenticating via
username/password but it has its limitations which you should be aware of.

Details can be found at :ref:`auth-config`

Allowed SCMs
============

The ``allowed_scms`` option in builder's config controls which SCMs (Source Control Management
systems) are allowed for building.
We recommend that every production environment choose a limited set of trusted sources.

Details of the ``allowed_scms`` option are covered under :ref:`scm-config`


Hub Policies
============

Hub policies are a powerful way for administrators to control Koji's behavior.
Koji's hub allows several different policies to be configured, some of which are
access control policies.

An access control policy is consulted by the hub to determine if an action should be allowed.
Such policies return results of ``deny`` or ``allow``.

Examples of access control polices are:

* tag: control which tag operations are allowed
* package_list: control which package list updates are allowed
* cg_import: control which content generator imports are allowed
* vm: control which windows build tasks are allowed
* dist_repo: control which distRepo tasks are allowed
* build_from_srpm: control whether builds from srpm are allowed
* build_from_repo_id: control whether builds from user-specified repos ids are allowed

Note that not all policies are access control policies.
The ``channel`` and ``volume`` policies are used to control which channels tasks go to
and which volumes build are stored on.

For more details see :doc:`defining_hub_policies`.

User Permissions
================

Every user can have a set of permissions which allow them to perform some actions directly.
These permissions may be checked directly by the hub, or they may be referenced in policies.

See :doc:`permissions` for details.
