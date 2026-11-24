VERSION := $(shell tr -d '[:space:]' < VERSION)
PKG := steward-$(VERSION)
HOST ?= root@steward-test.example.com
INSTALL_ARGS ?= --yes --build

.PHONY: release test deploy clean

release:
	@rm -rf dist/stage && mkdir -p dist/stage/$(PKG)
	COPYFILE_DISABLE=1 tar -cf - \
	  --exclude=./.git --exclude=./dist --exclude=./.env --exclude=./certs --exclude=./data --exclude=./backups \
	  --exclude=node_modules --exclude=./frontend/dist --exclude=__pycache__ --exclude=.pytest_cache \
	  --exclude=.DS_Store --exclude='._*' --exclude='*.whl' --exclude=./.github --exclude=./initial-admin-password.txt . \
	  | tar -xf - -C dist/stage/$(PKG)
	chmod +x dist/stage/$(PKG)/install.sh dist/stage/$(PKG)/upgrade.sh dist/stage/$(PKG)/scripts/*.sh
	COPYFILE_DISABLE=1 tar $(shell tar --version 2>/dev/null | grep -q bsdtar \
	    && echo --no-xattrs --no-mac-metadata --uid 0 --gid 0 --uname root --gname root \
	    || echo --owner=0 --group=0 --numeric-owner) \
	  -czf dist/$(PKG).tar.gz -C dist/stage $(PKG)
	@rm -rf dist/stage
	@echo "Built dist/$(PKG).tar.gz"

test:
	cd backend && python3 -m pytest -q
	cd frontend && npm run typecheck

# Ship the release package to a host and install it exactly like a customer would.
deploy: release
	scp dist/$(PKG).tar.gz $(HOST):/root/
	ssh $(HOST) 'rm -rf /root/$(PKG) && tar -xzf /root/$(PKG).tar.gz -C /root && /root/$(PKG)/install.sh $(INSTALL_ARGS)'

clean:
	rm -rf dist
