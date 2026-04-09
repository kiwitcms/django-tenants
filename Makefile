# Copyright (c) 2026 Alexander Todorov <atodorov@otb.bg>
#
# Licensed under GNU Affero General Public License v3 or later (AGPLv3+)
# https://www.gnu.org/licenses/agpl-3.0.html

.PHONY: package
package:
	rm -rf build/ dist/ kiwitcms_*.egg-info/
	python setup.py sdist
	python setup.py bdist_wheel
	twine check dist/*

.PHONY: upload
upload: package
	test -n "$(TWINE_USERNAME)" || exit 1
	test -n "$(TWINE_PASSWORD)" || exit 2
	twine upload dist/* --repository-url https://push.fury.io/kiwitcms
