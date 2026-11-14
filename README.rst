kiwitcms-django-tenants
=======================

This is a lean fork of https://github.com/django-tenants/django-tenants
started at version 3.8.0,
https://github.com/django-tenants/django-tenants/releases/tag/v3.7.8, commit 7d82923.

It reverts the changes made in
https://github.com/django-tenants/django-tenants/pull/997 b/c they cause existing
Kiwi TCMS tests to fail, https://github.com/django-tenants/django-tenants/issues/1155,
and appear to be much more disruptive than what appears initially!

This fork also removes any upstream functionality which isn't consumed in Kiwi TCMS
and focuses on maintainability!

Originally Kiwi TCMS needed the rest of the changes in order to become compatible with
Django 5.2, but we didn't want to risk breaking production instances hence the fork!
Since then it has taken a life of its own and is only fit as a building block for
https://github.com/kiwitcms/tenants/!


Changelog
=========


v3.8.3 (20 Sep 2026)
--------------------

- Add Django 6.1 support. Cherry-picked from upstream
  https://github.com/django-tenants/django-tenants/commit/2304f34


v3.8.2
------

- Raised the minimum supported Python version to 3.12.
- Updated the test matrix to Python 3.12 and 3.14, PostgreSQL 17 and 18,
  and Django 5.2 and 6.0.
- Fixed ``AppRegistryNotReady: Apps aren't loaded yet`` in the
  multiprocessing migration executor by explicitly using the ``fork``
  start method, which is required now that newer Python versions no
  longer default to ``fork`` on Linux.
