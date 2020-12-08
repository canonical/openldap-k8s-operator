#!/usr/bin/env python3
# Copyright 2020 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

from charmhelpers.core import host
import ops.lib
from ops.charm import (
    CharmBase,
    CharmEvents,
)
from ops.main import main
from ops.framework import (
    StoredState,
    EventBase,
    EventSource,
)
from ops.model import (
    ActiveStatus,
    MaintenanceStatus,
    WaitingStatus,
)
from leadership import RichLeaderData


pgsql = ops.lib.use("pgsql", 1, "postgresql-charmers@lists.launchpad.net")

logger = logging.getLogger(__name__)


DATABASE_NAME = 'openldap'


class OpenLDAPDBMasterAvailableEvent(EventBase):
    pass


class OpenLDAPCharmEvents(CharmEvents):
    """Custom charm events."""

    db_master_available = EventSource(OpenLDAPDBMasterAvailableEvent)


class OpenLDAPK8sCharm(CharmBase):
    _state = StoredState()

    on = OpenLDAPCharmEvents()

    def __init__(self, *args):
        super().__init__(*args)

        self.leader_data = RichLeaderData(self, "leader_data")

        self.framework.observe(self.on.start, self._configure_pod)
        self.framework.observe(self.on.config_changed, self._configure_pod)
        self.framework.observe(self.on.leader_elected, self._configure_pod)
        self.framework.observe(self.on.upgrade_charm, self._configure_pod)
        self.framework.observe(self.on.get_admin_password_action, self._on_get_admin_password_action)

        # database
        self._state.set_default(postgres=None)
        self.db = pgsql.PostgreSQLClient(self, 'db')
        self.framework.observe(self.db.on.database_relation_joined, self._on_database_relation_joined)
        self.framework.observe(self.db.on.master_changed, self._on_master_changed)
        self.framework.observe(self.on.db_master_available, self._configure_pod)

    def _on_database_relation_joined(self, event: pgsql.DatabaseRelationJoinedEvent):
        """Handle db-relation-joined."""
        if self.model.unit.is_leader():
            # Provide requirements to the PostgreSQL server.
            event.database = DATABASE_NAME  # Request database named mydbname
        elif event.database != DATABASE_NAME:
            # Leader has not yet set requirements. Defer, incase this unit
            # becomes leader and needs to perform that operation.
            event.defer()

    def _on_master_changed(self, event: pgsql.MasterChangedEvent):
        """Handle changes in the primary database unit."""
        if event.database != DATABASE_NAME:
            # Leader has not yet set requirements. Wait until next
            # event, or risk connecting to an incorrect database.
            return

        self._state.postgres = None
        if event.master is None:
            return

        self._state.postgres = {
            'dbname': event.master.dbname,
            'user': event.master.user,
            'password': event.master.password,
            'host': event.master.host,
            'port': event.master.port,
        }

        self.on.db_master_available.emit()

    def _make_pod_spec(self):
        """Return a pod spec with some core configuration."""
        config = self.model.config
        image_details = {
            'imagePath': config['image_path'],
        }
        if config['image_username']:
            image_details.update({'username': config['image_username'], 'password': config['image_password']})
        pod_config = self._make_pod_config()

        return {
            'version': 3,  # otherwise resources are ignored
            'containers': [
                {
                    'name': self.app.name,
                    'imageDetails': image_details,
                    'ports': [{'containerPort': 389, 'protocol': 'TCP'}],
                    'envConfig': pod_config,
                    'kubernetes': {
                        'readinessProbe': {'tcpSocket': {'port': 389}},
                    },
                }
            ],
        }

    def _on_get_admin_password_action(self, event):
        """Handle on get-admin-password action."""
        event.set_results(self.get_admin_password())

    def get_admin_password(self):
        """Get (or set if not yet created) an LDAP admin password."""
        try:
            return self.leader_data["admin_password"]
        except KeyError:
            pw = host.pwgen(40)
            self.leader_data["admin_password"] = pw
            return pw

    def _make_pod_config(self):
        """Return an envConfig with some core configuration."""
        pod_config = {
            'POSTGRES_NAME': self._state.postgres['dbname'],
            'POSTGRES_USER': self._state.postgres['user'],
            'POSTGRES_PASSWORD': self._state.postgres['password'],
            'POSTGRES_HOST': self._state.postgres['host'],
            'POSTGRES_PORT': self._state.postgres['port'],
        }

        pod_config['LDAP_ADMIN_PASSWORD'] = self.get_admin_password()

        return pod_config

    def _configure_pod(self, event):
        """Assemble the pod spec and apply it, if possible."""
        if not self._state.postgres:
            self.unit.status = WaitingStatus('Waiting for database relation')
            event.defer()
            return

        if not self.unit.is_leader():
            self.unit.status = ActiveStatus()
            return

        self.unit.status = MaintenanceStatus('Assembling pod spec')
        pod_spec = self._make_pod_spec()

        self.unit.status = MaintenanceStatus('Setting pod spec')
        self.model.pod.set_spec(pod_spec)
        self.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(OpenLDAPK8sCharm, use_juju_for_storage=True)
