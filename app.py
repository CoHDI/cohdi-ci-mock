# Copyright 2026 The CoHDI Authors.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import re
import time
import uuid
from flask import Flask, request

app = Flask(__name__)

CONFIG_PATH = "./config/resources.json"
K8S_DRIVER_NAMESPACE = "gpu-operator"
K8S_DRIVER_LABEL_SELECTOR = "app.kubernetes.io/component=nvidia-driver"
K8S_RESCAN_NAMESPACE = "composable-resource-operator-system"
K8S_RESCAN_POD_NAME = "cro-node-agent"
K8S_GPU_SMI_CMD = "/usr/bin/nvidia-smi -L"
K8S_RESCAN_CMD = "echo 1 > /sys/bus/pci/rescan"
K8S_LSPCI_CMD = "chroot /host-root lspci"
K8S_INIT_DRIVER_WAIT_TIMEOUT_SECONDS = int(os.environ.get("K8S_INIT_DRIVER_WAIT_TIMEOUT_SECONDS", "900"))
K8S_INIT_DRIVER_WAIT_INTERVAL_SECONDS = int(os.environ.get("K8S_INIT_DRIVER_WAIT_INTERVAL_SECONDS", "10"))
COMPOSABLE_DRA_NAMESPACE = "composable-dra"
COMPOSABLE_DRA_CONFIGMAP = "composable-dra-dds"
_K8S_REST_CLIENT = None
_K8S_EXEC_CLIENT = None
_K8S_STREAM = None
_CONFIG_RESOURCES = None
_MODEL_MAP = None
_ALLOCATED_RESOURCES_BY_MACHINE = {}
_DYNAMIC_RESOURCE_SERIALS = set()
_DYNAMIC_RESOURCE_SERIALS_BY_MACHINE = {}
_VISIBLE_RESOURCE_SERIALS_BY_MACHINE = {}

def load_config_resources(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("resources", [])


def get_config_resources():
    global _CONFIG_RESOURCES
    if _CONFIG_RESOURCES is None:
        _CONFIG_RESOURCES = load_config_resources(CONFIG_PATH)
    if isinstance(_CONFIG_RESOURCES, tuple):
        return []
    return _CONFIG_RESOURCES


def get_k8s_client():
    global _K8S_REST_CLIENT, _K8S_EXEC_CLIENT, _K8S_STREAM
    if _K8S_REST_CLIENT is not None and _K8S_EXEC_CLIENT is not None:
        return _K8S_REST_CLIENT, _K8S_EXEC_CLIENT, _K8S_STREAM, None

    try:
        from kubernetes import client, config, stream
    except Exception as exc:
        return None, None, None, f"kubernetes client not available: {exc}"

    try:
        config.load_incluster_config()
    except Exception as exc:
        return None, None, None, f"failed to load kubernetes config: {exc}"

    _K8S_REST_CLIENT = client.CoreV1Api(client.ApiClient())

    _K8S_EXEC_CLIENT = client.CoreV1Api(client.ApiClient())

    _K8S_STREAM = stream.stream
    return _K8S_REST_CLIENT, _K8S_EXEC_CLIENT, _K8S_STREAM, None



def get_model_map():
    global _MODEL_MAP
    if _MODEL_MAP is not None:
        return _MODEL_MAP
    v1_rest, _, _, err = get_k8s_client()
    if err:
        _MODEL_MAP = {}
        return _MODEL_MAP
    try:
        configmap = v1_rest.read_namespaced_config_map(
            name=COMPOSABLE_DRA_CONFIGMAP, namespace=COMPOSABLE_DRA_NAMESPACE
        )
    except Exception:
        _MODEL_MAP = {}
        return _MODEL_MAP
    device_info = None
    if configmap and configmap.data:
        device_info = configmap.data.get("device-info")
    if not device_info:
        _MODEL_MAP = {}
        return _MODEL_MAP
    try:
        import yaml
    except Exception:
        _MODEL_MAP = {}
        return _MODEL_MAP
    try:
        device_list = yaml.safe_load(device_info) or []
    except Exception:
        _MODEL_MAP = {}
        return _MODEL_MAP
    model_map = {}
    for item in device_list:
        if not isinstance(item, dict):
            continue
        cdi_model = item.get("cdi-model-name")
        attrs = item.get("dra-attributes") or {}
        product = attrs.get("productName")
        if cdi_model and product:
            model_map[product] = cdi_model
    _MODEL_MAP = model_map
    return _MODEL_MAP


def normalize_machine_uuid(provider_id):
    if not provider_id:
        return None
    if "://" in provider_id:
        return provider_id.rsplit("/", 1)[-1]
    return provider_id


def find_node_name_by_machine_uuid(v1, machine_uuid):
    nodes = v1.list_node().items
    for node in nodes:
        provider_id = (node.spec.provider_id if node.spec else None) or ""
        machine_id = normalize_machine_uuid(provider_id)
        if machine_id == machine_uuid:
            return node.metadata.name
    return None


def find_driver_pod_on_node(v1, node_name):
    pods = v1.list_namespaced_pod(
        namespace=K8S_DRIVER_NAMESPACE,
        field_selector=f"spec.nodeName={node_name}",
        label_selector=K8S_DRIVER_LABEL_SELECTOR,
    )
    for pod in pods.items:
        if is_pod_ready(pod):
            return pod.metadata.name
    return None


def is_pod_ready(pod):
    if not pod.status or pod.status.phase != "Running":
        return False
    for condition in pod.status.conditions or []:
        if condition.type == "Ready" and condition.status == "True":
            return True
    return False


def find_rescan_pod_on_node(v1, node_name):
    pods = v1.list_namespaced_pod(
        namespace=K8S_RESCAN_NAMESPACE, field_selector=f"spec.nodeName={node_name}"
    )
    for pod in pods.items:
        name = pod.metadata.name if pod.metadata else ""
        if K8S_RESCAN_POD_NAME in name and pod.status and pod.status.phase == "Running":
            return name
    return None


def exec_on_pod(v1, stream_fn, namespace, pod_name, command):
    return stream_fn(
        v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=["/bin/sh", "-c", command],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def run_driver_command_on_node(v1_rest, v1_exec, stream_fn, node_name, command):
    pod_name = find_driver_pod_on_node(v1_rest, node_name)
    if not pod_name:
        return None
    return exec_on_pod(v1_exec, stream_fn, K8S_DRIVER_NAMESPACE, pod_name, command)


def run_rescan_command_on_node(v1_rest, v1_exec, stream_fn, node_name, command):
    pod_name = find_rescan_pod_on_node(v1_rest, node_name)
    if not pod_name:
        return None
    return exec_on_pod(v1_exec, stream_fn, K8S_RESCAN_NAMESPACE, pod_name, command)


def run_rescan_on_node(v1_rest, v1_exec, stream_fn, node_name):
    return run_rescan_command_on_node(
        v1_rest, v1_exec, stream_fn, node_name, K8S_RESCAN_CMD
    ) is not None


def node_has_nvidia_pci(v1_rest, v1_exec, stream_fn, node_name):
    output = run_rescan_command_on_node(
        v1_rest, v1_exec, stream_fn, node_name, K8S_LSPCI_CMD
    )
    if output is None:
        return None
    normalized_output = output.lower()
    return "nvidia" in normalized_output or "10de:" in normalized_output


def run_nvidia_smi_on_node(v1_rest, v1_exec, stream_fn, node_name):
    return run_driver_command_on_node(
        v1_rest, v1_exec, stream_fn, node_name, K8S_GPU_SMI_CMD
    )


def wait_for_node_gpus(v1_rest, v1_exec, stream_fn, node_name):
    deadline = time.time() + K8S_INIT_DRIVER_WAIT_TIMEOUT_SECONDS
    while time.time() <= deadline:
        output = run_nvidia_smi_on_node(v1_rest, v1_exec, stream_fn, node_name)
        if output:
            gpus = parse_nvidia_smi_output(output)
            if gpus:
                return gpus
        time.sleep(K8S_INIT_DRIVER_WAIT_INTERVAL_SECONDS)
    return []


def parse_nvidia_smi_output(output):
    gpus = []
    for line in output.splitlines():
        match = re.search(r"GPU\s+\d+:\s+(.+?)\s+\(UUID:\s+([^)]+)\)", line)
        if not match:
            continue
        model = match.group(1).strip()
        uuid = match.group(2).strip()
        gpus.append({"model": model, "uuid": uuid})
    return gpus


def register_dynamic_resources(gpus, machine_uuid=None):
    model_map = get_model_map()
    resources = get_config_resources()
    existing_serials = {res.get("res_serial_num") for res in resources}
    for gpu in gpus:
        serial = gpu.get("uuid")
        if not serial:
            continue
        _DYNAMIC_RESOURCE_SERIALS.add(serial)
        if machine_uuid:
            _DYNAMIC_RESOURCE_SERIALS_BY_MACHINE.setdefault(machine_uuid, set()).add(serial)
            _VISIBLE_RESOURCE_SERIALS_BY_MACHINE.setdefault(machine_uuid, set()).add(serial)
        if serial in existing_serials:
            continue
        model_raw = gpu.get("model")
        model = model_map.get(model_raw, model_raw)
        resources.append(
            {
                "res_uuid": str(uuid.uuid4()),
                "res_serial_num": serial,
                "model": model,
            }
        )
        existing_serials.add(serial)


def format_resource(res):
    return {
        "res_uuid": res.get("res_uuid"),
        "res_name": "dummy",
        "res_type": "gpu",
        "res_status": 4,
        "res_op_status": "0",
        "res_serial_num": res.get("res_serial_num"),
        "res_spec": {
            "condition": [
                {
                    "column": "model",
                    "operator": "eq",
                    "value": res.get("model"),
                }
            ]
        },
    }


def get_allocated_resources(machine_uuid):
    return _ALLOCATED_RESOURCES_BY_MACHINE.get(machine_uuid, [])


def get_dynamic_serials(machine_uuid=None):
    if not machine_uuid:
        return _DYNAMIC_RESOURCE_SERIALS
    return _DYNAMIC_RESOURCE_SERIALS_BY_MACHINE.get(machine_uuid, set())


def get_visible_serials(machine_uuid):
    return _VISIBLE_RESOURCE_SERIALS_BY_MACHINE.get(machine_uuid, set())


def set_resources_visible(machine_uuid, resources):
    visible_serials = _VISIBLE_RESOURCE_SERIALS_BY_MACHINE.setdefault(machine_uuid, set())
    for res in resources:
        serial = res.get("res_serial_num")
        if serial:
            visible_serials.add(serial)


def set_resource_hidden(machine_uuid, resource_uuid=None):
    if machine_uuid not in _VISIBLE_RESOURCE_SERIALS_BY_MACHINE:
        return
    if not resource_uuid:
        _VISIBLE_RESOURCE_SERIALS_BY_MACHINE.pop(machine_uuid, None)
        return
    for res in get_config_resources():
        if res.get("res_uuid") == resource_uuid:
            _VISIBLE_RESOURCE_SERIALS_BY_MACHINE[machine_uuid].discard(
                res.get("res_serial_num")
            )
            break


def collect_node_resources(machine_uuid=None):
    resources = []
    seen_serials = set()

    if machine_uuid:
        visible_serials = get_visible_serials(machine_uuid)
        for res in get_config_resources():
            serial = res.get("res_serial_num")
            if serial not in visible_serials:
                continue
            resources.append(format_resource(res))
            seen_serials.add(serial)

        for res in get_allocated_resources(machine_uuid):
            serial = res.get("res_serial_num")
            if serial in seen_serials:
                continue
            resources.append(format_resource(res))
            seen_serials.add(serial)

    return resources, None


def allocate_resources(machine_uuid, model, res_num):
    allocated = _ALLOCATED_RESOURCES_BY_MACHINE.setdefault(machine_uuid, [])
    allocated_serials = {
        res.get("res_serial_num")
        for resources in _ALLOCATED_RESOURCES_BY_MACHINE.values()
        for res in resources
    }
    candidates = []
    dynamic_serials = get_dynamic_serials(machine_uuid)
    for res in get_config_resources():
        serial = res.get("res_serial_num")
        if serial not in dynamic_serials:
            continue
        if res.get("model") != model or serial in allocated_serials:
            continue
        candidates.append(res)
        if len(candidates) == res_num:
            break

    if len(candidates) < res_num:
        return []

    allocated.extend(candidates)
    set_resources_visible(machine_uuid, candidates)
    return [format_resource(res) for res in candidates]


def init_dynamic_resources():
    v1_rest, v1_exec, stream_fn, err = get_k8s_client()
    if err:
        return
    nodes = v1_rest.list_node().items
    for node in nodes:
        node_name = node.metadata.name
        provider_id = (node.spec.provider_id if node.spec else None) or ""
        machine_uuid = normalize_machine_uuid(provider_id)
        if not run_rescan_on_node(v1_rest, v1_exec, stream_fn, node_name):
            continue
        if node_has_nvidia_pci(v1_rest, v1_exec, stream_fn, node_name) is not True:
            continue
        gpus = wait_for_node_gpus(v1_rest, v1_exec, stream_fn, node_name)
        register_dynamic_resources(gpus, machine_uuid)


def collect_machine_list():
    v1_rest, v1_exec, stream_fn, err = get_k8s_client()
    if err:
        return None, err

    nodes = v1_rest.list_node().items
    machines = []
    for _idx, node in enumerate(nodes, start=1):
        provider_id = (node.spec.provider_id if node.spec else None) or ""
        machine_uuid = normalize_machine_uuid(provider_id)
        pci_present = node_has_nvidia_pci(v1_rest, v1_exec, stream_fn, node.metadata.name)
        if pci_present is False:
            set_resource_hidden(machine_uuid)
        resources, res_err = collect_node_resources(machine_uuid)
        if res_err:
            return None, res_err
        machines.append(
            {
                "fabric_uuid": "dummy",
                "fabric_id": 1,
                "mach_uuid": machine_uuid,
                "mach_id": 1,
                "mach_name": "dummy",
                "mach_owner": "dummy",
                "resources": resources,
            }
        )
    return {"data": {"machines": machines}}, None


def collect_machine_detail(machine_uuid):
    data, err = collect_machine_list()
    if err:
        return None, err
    detail = []
    for machine in data["data"]["machines"]:
        if machine["mach_uuid"] == machine_uuid:
            detail.append(
                {
                    "fabric_uuid": "dummy",
                    "fabric_id": 1,
                    "mach_uuid": machine["mach_uuid"],
                    "mach_id": 1,
                    "mach_name": "m1",
                    "mach_owner": "dummy",
                    "mach_status": 15,
                    "mach_op_status": "00",
                    "mach_status_detail": "dummy",
                    "tenant_uuid": "dummy",
                    "resources": machine["resources"],
                }
            )
            return {"data": {"machines": detail}}, None
    return None, "machine not found"

def extract_model_from_request():
    condition_raw = request.args.get("condition")
    if condition_raw:
        try:
            import urllib.parse
            import ast
        except Exception:
            condition_obj = None
        else:
            decoded = urllib.parse.unquote_plus(condition_raw)
            try:
                condition_obj = json.loads(decoded)
            except Exception:
                try:
                    condition_obj = ast.literal_eval(decoded)
                except Exception:
                    condition_obj = None
        if isinstance(condition_obj, dict):
            if condition_obj.get("column") == "model":
                return condition_obj.get("value")
    return None

def extract_model_from_body():
    payload = request.get_json(silent=True) or {}
    res_spec = payload.get("res_spec", {})
    conditions = res_spec.get("condition") or res_spec.get("conditions") or []
    for cond in conditions:
        if cond.get("column") == "model":
            return cond.get("value")
    tenants = payload.get("tenants", {})
    machines = tenants.get("machines", [])
    for machine in machines:
        resources = machine.get("resources", [])
        for resource in resources:
            res_specs = resource.get("res_specs", [])
            for res_spec in res_specs:
                res_spec_cond = res_spec.get("res_spec", {}).get("condition", [])
                for cond in res_spec_cond:
                    if cond.get("column") == "model":
                        return cond.get("value")
    return None

def extract_res_num_from_request():
    payload = request.get_json(silent=True) or {}
    res_num = None
    tenants = payload.get("tenants", {})
    machines = tenants.get("machines", [])
    for machine in machines:
        resources = machine.get("resources", [])
        for resource in resources:
            res_specs = resource.get("res_specs", [])
            for res_spec in res_specs:
                if "res_num" in res_spec:
                    res_num = res_spec.get("res_num")
                    break
            if res_num is not None:
                break
        if res_num is not None:
            break
    if res_num is None:
        return None
    try:
        return int(res_num)
    except (TypeError, ValueError):
        return None

@app.route("/fabric_manager/api/v1/machines", methods=["GET"])
def get_machine_list():
    data, err = collect_machine_list()
    if err:
        return json.dumps({"error": err}), 500, {"Content-Type": "application/json"}
    return json.dumps(data, ensure_ascii=False), 200, {"Content-Type": "application/json"}


@app.route("/fabric_manager/api/v1/machines/<m>", methods=["GET"])
def get_machine(m):
    data, err = collect_machine_detail(m)
    if err:
        return json.dumps({"error": err}), 404, {"Content-Type": "application/json"}
    return json.dumps(data, ensure_ascii=False), 200, {"Content-Type": "application/json"}


@app.route(
    "/fabric_manager/api/v1/machines/<m>/available-reserved-resources", methods=["GET"]
)
def get_available_machines(m):
    model = extract_model_from_request()
    if not model:
        return json.dumps({"error": "invalid_parameter"}), 400, {"Content-Type": "application/json"}
    attached_serials = {
        res.get("res_serial_num")
        for resources in _ALLOCATED_RESOURCES_BY_MACHINE.values()
        for res in resources
    }
    dynamic_serials = get_dynamic_serials(m)
    available_count = 0
    for res in get_config_resources():
        if res.get("res_serial_num") not in dynamic_serials:
            continue
        if res.get("model") != model:
            continue
        if res.get("res_serial_num") in attached_serials:
            continue
        available_count += 1
    return json.dumps({"reserved_res_num_per_fabric": available_count}, ensure_ascii=False), 200, {
        "Content-Type": "application/json"
    }


@app.route("/fabric_manager/api/v1/machines/<m>/update", methods=["PATCH"])
def patch_devices_fm(m):
    v1_rest, v1_exec, stream_fn, err = get_k8s_client()
    if err:
        return json.dumps({"error": err}), 500, {"Content-Type": "application/json"}
    node_name = find_node_name_by_machine_uuid(v1_rest, m)
    if not node_name:
        return json.dumps({"error": "machine not found"}), 404, {"Content-Type": "application/json"}
    model = extract_model_from_body()
    res_num = extract_res_num_from_request()
    if not model or res_num is None or res_num < 1:
        return json.dumps({"error": "invalid_parameter"}), 400, {"Content-Type": "application/json"}

    if not run_rescan_on_node(v1_rest, v1_exec, stream_fn, node_name):
        return json.dumps({"error": "rescan pod not found"}), 500, {"Content-Type": "application/json"}

    response_resources = allocate_resources(m, model, res_num)
    if len(response_resources) < res_num:
        return json.dumps({"error": "insufficient_resources"}), 404, {"Content-Type": "application/json"}

    response = {
        "data": {
            "machines": [
                {
                    "mach_uuid": m,
                    "fabric_uuid": "dummy",
                    "fabric_id": 1,
                    "mach_id": 1,
                    "mach_name": "dummy",
                    "resources": response_resources,
                }
            ]
        }
    }
    return json.dumps(response, ensure_ascii=False), 200, {"Content-Type": "application/json"}


@app.route("/fabric_manager/api/v1/machines/<m>/update", methods=["DELETE"])
def delete_devices_fm(m):
    payload = request.get_json(silent=True) or {}
    resource_uuid = None
    tenants = payload.get("tenants", {})
    machines = tenants.get("machines", [])
    for machine in machines:
        resources = machine.get("resources", [])
        for resource in resources:
            res_specs = resource.get("res_specs", [])
            for res_spec in res_specs:
                resource_uuid = res_spec.get("res_uuid")
                if resource_uuid:
                    break
            if resource_uuid:
                break
        if resource_uuid:
            break

    if resource_uuid and m in _ALLOCATED_RESOURCES_BY_MACHINE:
        _ALLOCATED_RESOURCES_BY_MACHINE[m] = [
            res
            for res in _ALLOCATED_RESOURCES_BY_MACHINE[m]
            if res.get("res_uuid") != resource_uuid
        ]
        set_resource_hidden(m, resource_uuid)
    elif not resource_uuid:
        _ALLOCATED_RESOURCES_BY_MACHINE.pop(m, None)
        set_resource_hidden(m)
    return "{}", 200, {"Content-Type": "application/json"}

@app.route("/id_manager/realms/<realm>/protocol/openid-connect/token", methods=["POST"])
def get_token(realm):
    response = {
        "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOiAzMjUwMzY4MDAwMCwgInByZWZlcnJlZF91c2VybmFtZSI6ICJ0ZXN0In0K.dGVzdAo",
        "expires_in": 300,
        "refresh_expires_in": 2,
        "refresh_token": "token2",
        "token_type": "Bearer",
        "id_token": "token3",
        "not-before-policy": 3,
        "session_state": "efffca5t4",
        "scope": "test profile"
    }
    return json.dumps(response, ensure_ascii=False), 200, {"Content-Type": "application/json"}

if __name__ == "__main__":
    init_dynamic_resources()
    app.run(host='0.0.0.0', port=443, ssl_context=('certs/server.crt', 'certs/server.key'))
