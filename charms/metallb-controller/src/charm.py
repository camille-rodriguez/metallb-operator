#!/usr/bin/env python3
"""Controller component for the MetalLB bundle."""

import logging
import json
import os
from hashlib import md5

from oci_image import OCIImageResource, OCIImageResourceError

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    WaitingStatus,
)

import utils

logger = logging.getLogger(__name__)


class MetalLBControllerCharm(CharmBase):
    """MetalLB Controller Charm."""

    _stored = StoredState()
    _authed = False

    def __init__(self, *args):
        """Charm initialization for events observation."""
        super().__init__(*args)
        if not self.unit.is_leader():
            self.unit.status = WaitingStatus("Waiting for leadership")
            return
        self.image = OCIImageResource(self, 'metallb-controller-image')
        self.framework.observe(self.on.install, self._on_start)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.leader_elected, self._on_start)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.remove, self._on_remove)
        # -- initialize states --
        self._stored.set_default(k8s_objects_created=False)
        self._stored.set_default(started=False)
        self._stored.set_default(config_hash=self._config_hash())
        # -- base values --
        self._stored.set_default(namespace=os.environ["JUJU_MODEL_NAME"])

    def _config_hash(self):
        data = json.dumps({
            'iprange': self.model.config['iprange'],
        }, sort_keys=True)
        return md5(data.encode('utf8')).hexdigest()

    def _on_start(self, event):
        """Occurs upon install, start, upgrade, and possibly config changed."""
        if self._stored.started:
            return
        if not self._k8s_auth():
            event.defer()
            return
        self.unit.status = MaintenanceStatus("Fetching image information")
        logging.info("Fetching OCI image information")
        try:
            image_info = self.image.fetch()
        except OCIImageResourceError:
            logging.exception('An error occured while fetching the image info')
            self.unit.status = BlockedStatus("Error fetching image information")
            return

        if not self._stored.k8s_objects_created:
            self.unit.status = MaintenanceStatus("Creating supplementary "
                                                 "Kubernetes objects")
            logging.info("Creating Kubernetes objects with K8s API")
            utils.create_k8s_objects(self._stored.namespace)
            self._stored.k8s_objects_created = True
        
        self.unit.status = MaintenanceStatus("Configuring pod")
        self._set_configmap()
        # Get the controller container so we can configure/manipulate it # Are we doing anything with it ?
        container = self.unit.get_container("metallb-controller")
        # Create a new config layer
        layer = self._controller_layer()

        logging.debug('testing here patch stateful set')
        self._patch_stateful_set()
        self.unit.status = ActiveStatus()
        self._stored.started = True

    def _on_upgrade(self, event):
        """Occurs when new charm code or image info is available."""
        if not self._k8s_auth():
            event.defer()
            return
        self._stored.started = False
        self._on_start(event)

    def _on_config_changed(self, event):
        """Handles changes in the IP range or protocol configuration"""
        if not self._k8s_auth():
            event.defer()
            return
        if self.model.config['protocol'] != 'layer2':
            self.unit.status = BlockedStatus('Invalid protocol; '
                                             'only "layer2" currently supported')
            return
        current_config_hash = self._config_hash()
        if current_config_hash != self._stored.config_hash:
            self._stored.started = False
            self._stored.config_hash = current_config_hash
            self._on_start(event)

    def _on_remove(self, event):
        """Remove of artifacts created by the K8s API."""
        if not self._k8s_auth():
            event.defer()
            return
        self.unit.status = MaintenanceStatus("Removing supplementary "
                                             "Kubernetes objects")
        utils.remove_k8s_objects(self._stored.namespace)
        self.unit.status = MaintenanceStatus("Removing pod")
        self._stored.started = False
        self._stored.k8s_objects_created = False

    def _controller_layer(self):
        """Returns a Pebble configration layer for MetalLB Controller"""
        return {
            "summary": "metallb controller layer",
            "description": "pebble confid layer for metallb controller",
            "services": {
                "controller": {
                    "summary": "controller",
                    "startup": "enabled",
                }
            }
        }

    def _set_configmap(self):
        CONFIG_MAP_NAME = 'config'
        iprange = self.model.config["iprange"].split(",")
        iprange_map = "address-pools:\n- name: default\n  protocol: layer2\n  addresses:\n"
        for range in iprange:
            iprange_map += "  - " + range + "\n"

        utils.set_config_map(CONFIG_MAP_NAME, self._stored.namespace, iprange_map)

    def _patch_stateful_set(self) -> None:
        """Patch the StatefulSet created by Juju"""
        self.unit.status = MaintenanceStatus("Patching StatefulSet for additional k8s config")
        # Get an API client
        api = client.AppsV1Api(client.ApiClient())
        # Read the StatefulSet we're deployed into
        name = "metallb-controller"
        namespace = self.model.name
        statefulset_body = api.read_namespaced_stateful_set(name=name, namespace=namespace)
        logging.debug(statefulset_body)
        # Config missing annotations - WIP, doens't seem to work yet
        statefulset_body.metadata.annotations['prometheus.io/port'] = '7472'
        statefulset_body.metadata.annotations['prometheus.io/scrape'] = 'true'
        # Config changes to the ports exposed
        ports = [client.V1ContainerPort(container_port = 7472, name = 'monitoring')]
        for container in statefulset_body.spec.template.spec.containers:
            if container.name == 'metallb-controller':
                container.ports = ports
        with client.ApiClient() as api_client:
            api_instance = client.AppsV1Api(api_client)
            try:
                logging.debug(statefulset_body)
                api_response = api_instance.patch_namespaced_stateful_set(name, namespace, body=statefulset_body, pretty=True)
                logging.debug(api_response)
            except ApiException as err:
                    raise
    
    def _k8s_auth(self):
        """Authenticate to Kubernetes"""
        if self._authed:
            return True
        # TODO: Remove this workaround when bug LP:1892255 is fixed
        from pathlib import Path
        os.environ.update(
            dict(
                e.split("=")
                for e in Path("/proc/1/environ").read_text().split("\x00")
                if "KUBERNETES_SERVICE" in e
            )
        )
        # end workaround
        # Authenticate against the Kubernetes API using a mounted ServiceAccount token
        config.load_incluster_config()
        # Test the service account we've got for sufficient perms
        auth_api = client.RbacAuthorizationV1Api(client.ApiClient())

        try:
            auth_api.list_cluster_role()
        except client.rest.ApiException as e:
            logging.debug(e)
            print(e)
            if e.status == 401:
                # If we can't read a cluster role, we don't have enough permissions
                self.unit.status = BlockedStatus("Run juju trust on this application to continue")
                return False
            else:
                raise e
        self._authed = True
        return True

if __name__ == "__main__":
    main(MetalLBControllerCharm)
