kiwitcms-django-tenants
=======================

This is a fork of https://github.com/django-tenants/django-tenants at version 3.8.0,
https://github.com/django-tenants/django-tenants/releases/tag/v3.7.8, commit 7d82923.

It reverts the changes made in
https://github.com/django-tenants/django-tenants/pull/997 b/c they cause existing
Kiwi TCMS tests to fail and appear to be much more disruptive than what appears initially!
Kiwi TCMS needs the rest of the changes in order to become compatible with Django 5.2,
but we don't want to risk breaking production instances hence the fork!

For the full list of changes between the last known working version and the original 3.8.0 see:
https://github.com/django-tenants/django-tenants/compare/v3.7.0...v3.7.8

Changelog
=========

v3.8.2
------

- Raised the minimum supported Python version to 3.12.
- Updated the test matrix to Python 3.12 and 3.14, PostgreSQL 17 and 18,
  and Django 5.2 and 6.0.
- Fixed ``AppRegistryNotReady: Apps aren't loaded yet`` in the
  multiprocessing migration executor by explicitly using the ``fork``
  start method, which is required now that newer Python versions no
  longer default to ``fork`` on Linux.
