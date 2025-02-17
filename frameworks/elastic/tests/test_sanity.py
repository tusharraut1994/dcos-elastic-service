import logging
from typing import Any, Dict, Iterator, List, Set

from toolz import get_in
import pytest
import base64

import sdk_cmd
import sdk_hosts
import sdk_install
import sdk_metrics
import sdk_networks
import sdk_plan
import sdk_service
import sdk_tasks
import sdk_utils
from tests import config

log = logging.getLogger(__name__)

package_name = config.PACKAGE_NAME
service_name = sdk_utils.get_foldered_name(config.SERVICE_NAME)
current_expected_task_count = config.DEFAULT_TASK_COUNT
current_non_node_task_count = config.DEFAULT_TASK_COUNT - config.DEFAULT_NODES_COUNT
kibana_package_name = config.KIBANA_PACKAGE_NAME
kibana_service_name = config.KIBANA_SERVICE_NAME
kibana_timeout = config.KIBANA_DEFAULT_TIMEOUT
index_name = config.DEFAULT_INDEX_NAME
index_type = config.DEFAULT_INDEX_TYPE
index = config.DEFAULT_SETTINGS_MAPPINGS


@pytest.fixture(scope="module", autouse=True)
def configure_package(configure_security: None) -> Iterator[None]:
    try:
        log.info("Ensure elasticsearch and kibana are uninstalled...")
        sdk_install.uninstall(kibana_package_name, kibana_package_name)
        sdk_install.uninstall(package_name, service_name)

        config.test_upgrade(
            package_name,
            service_name,
            config.DEFAULT_NODES_COUNT,
            current_expected_task_count,
            from_options={"service": {"name": service_name}},
        )

        yield  # let the test session execute
    finally:
        log.info("Clean up elasticsearch and kibana...")
        sdk_install.uninstall(kibana_package_name, kibana_package_name)
        sdk_install.uninstall(package_name, service_name)


@pytest.fixture(autouse=True)
def pre_test_setup() -> None:
    sdk_tasks.check_running(service_name, current_expected_task_count)
    config.wait_for_expected_nodes_to_exist(
        service_name=service_name,
        task_count=current_expected_task_count - current_non_node_task_count,
    )


@pytest.fixture
def default_populated_index() -> None:
    config.delete_index(index_name, service_name=service_name)
    config.create_index(index_name, index, service_name=service_name)
    config.create_document(
        index_name, index_type, 1, {"name": "Loren", "role": "developer"}, service_name=service_name
    )


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_pod_replace_then_immediate_config_update() -> None:
    sdk_cmd.svc_cli(package_name, service_name, "pod replace data-0")

    plugins = "analysis-phonetic"

    sdk_service.update_configuration(
        package_name,
        service_name,
        {"service": {"update_strategy": "parallel"}, "elasticsearch": {"plugins": plugins}},
        current_expected_task_count,
    )

    # Ensure all nodes, especially data-0, get launched with the updated config.
    config.check_elasticsearch_plugin_installed(plugins, service_name=service_name)
    sdk_plan.wait_for_completed_deployment(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.sanity
def test_endpoints() -> None:
    # Check that we can reach the scheduler via admin router, and that returned endpoints are
    # sanitized.
    for endpoint in config.ENDPOINT_TYPES:
        endpoints = sdk_networks.get_endpoint(package_name, service_name, endpoint)
        host = endpoint.split("-")[0]  # 'coordinator-http' => 'coordinator'
        assert endpoints["dns"][0].startswith(sdk_hosts.autoip_host(service_name, host + "-0-node"))
        assert endpoints["vip"].startswith(sdk_hosts.vip_host(service_name, host))

    sdk_plan.wait_for_completed_deployment(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.sanity
def test_indexing(default_populated_index: None) -> None:
    indices_stats = config.get_elasticsearch_indices_stats(index_name, service_name=service_name)
    assert indices_stats["_all"]["primaries"]["docs"]["count"] == 1
    doc = config.get_document(index_name, index_type, 1, service_name=service_name)
    assert doc["_source"]["name"] == "Loren"

    sdk_plan.wait_for_completed_deployment(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.sanity
@pytest.mark.dcos_min_version("1.13")
def test_metrics() -> None:
    metrics = sdk_metrics.wait_for_metrics_from_cli("exporter-0-node", 60)

    elastic_metrics = list(non_zero_elastic_metrics(metrics))
    assert len(elastic_metrics) > 0

    node_types = ["master", "data", "coordinator"]
    node_names = get_node_names_from_metrics(elastic_metrics)
    for node_type in node_types:
        assert len(list(filter(lambda x: x.startswith(node_type), node_names))) > 0


def non_zero_elastic_metrics(metrics: List[Dict[Any, Any]]):
    for metric in metrics:
        if metric["name"].startswith("elasticsearch") and metric["value"] != 0:
            yield metric


def get_node_names_from_metrics(metrics: List[Dict[Any, Any]]) -> Set[str]:
    names = set()
    for metric in metrics:
        if "name" in metric["tags"]:
            names.add(metric["tags"]["name"])
    return names


@pytest.mark.incremental
@pytest.mark.sanity
def test_custom_yaml_base64() -> None:
    # Apply this custom YAML block as a base64-encoded string:

    # cluster:
    #   routing:
    #     allocation:
    #       node_initial_primaries_recoveries: 3
    # script.allowed_contexts: ["score", "update"]
    base64_elasticsearch_yml = "".join(
        [
            "Y2x1c3RlcjoKICByb3V0aW5nOgogICAgYWxsb2NhdGlvbjoKICAgICAgbm9kZV9pbml0aWFsX3Bya",
            "W1hcmllc19yZWNvdmVyaWVzOiAzCnNjcmlwdC5hbGxvd2VkX2NvbnRleHRzOiBbInNjb3JlIiwgInV",
            "wZGF0ZSJd",
        ]
    )

    sdk_service.update_configuration(
        package_name,
        service_name,
        {"elasticsearch": {"custom_elasticsearch_yml": base64_elasticsearch_yml}},
        current_expected_task_count,
    )

    # We're testing two things here:

    # 1. The default value for `cluster.routing.allocation.node_initial_primaries_recoveries` is 4.
    # Here we want to make sure that the end-result for the multiple YAML/Mustache compilation steps
    # results in a valid elasticsearch.yml file, with the correct setting value.
    config.check_custom_elasticsearch_cluster_setting(
        service_name, ["cluster", "routing", "allocation", "node_initial_primaries_recoveries"], "3"
    )

    # 2. `script.allowed_contexts` has an "array of strings" value defined in the custom YAML. Here
    # we're also making sure that the end-result for the multiple YAML/Mustache compilation steps
    # results in a valid elasticsearch.yml file, but with a trickier compilation case due to the
    # setting value being an array of strings.
    config.check_custom_elasticsearch_cluster_setting(
        service_name, ["script", "allowed_contexts"], ["score", "update"]
    )


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_losing_and_regaining_index_health(default_populated_index: None) -> None:
    config.check_elasticsearch_index_health(index_name, "green", service_name=service_name)
    sdk_cmd.kill_task_with_pattern(
        "data__.*Elasticsearch",
        "nobody",
        agent_host=sdk_tasks.get_service_tasks(service_name, "data-0-node")[0].host,
    )
    config.check_elasticsearch_index_health(index_name, "yellow", service_name=service_name)
    config.check_elasticsearch_index_health(index_name, "green", service_name=service_name)

    sdk_plan.wait_for_completed_deployment(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_master_reelection() -> None:
    initial_master = config.get_elasticsearch_master(service_name=service_name)
    sdk_cmd.kill_task_with_pattern(
        "master__.*Elasticsearch",
        "nobody",
        agent_host=sdk_tasks.get_service_tasks(service_name, initial_master)[0].host,
    )
    sdk_plan.wait_for_in_progress_recovery(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)
    config.wait_for_expected_nodes_to_exist(service_name=service_name)
    new_master = config.get_elasticsearch_master(service_name=service_name)
    assert new_master.startswith("master") and new_master != initial_master

    sdk_plan.wait_for_completed_deployment(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_master_node_replace() -> None:
    # Ideally, the pod will get placed on a different agent. This test will verify that the
    # remaining two masters find the replaced master at its new IP address. This requires a
    # reasonably low TTL for Java DNS lookups.
    sdk_cmd.svc_cli(package_name, service_name, "pod replace master-0")
    sdk_plan.wait_for_in_progress_recovery(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_data_node_replace() -> None:
    sdk_cmd.svc_cli(package_name, service_name, "pod replace data-0")
    sdk_plan.wait_for_in_progress_recovery(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_coordinator_node_replace() -> None:
    sdk_cmd.svc_cli(package_name, service_name, "pod replace coordinator-0")
    sdk_plan.wait_for_in_progress_recovery(service_name)
    sdk_plan.wait_for_completed_recovery(service_name)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
@pytest.mark.timeout(15 * 60)
def test_plugin_install_and_uninstall(default_populated_index: None) -> None:
    plugins = "analysis-icu"

    sdk_service.update_configuration(
        package_name,
        service_name,
        {"elasticsearch": {"plugins": plugins}},
        current_expected_task_count,
    )

    config.check_elasticsearch_plugin_installed(plugins, service_name=service_name)

    sdk_service.update_configuration(
        package_name, service_name, {"elasticsearch": {"plugins": ""}}, current_expected_task_count
    )

    config.check_elasticsearch_plugin_uninstalled(plugins, service_name=service_name)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_add_ingest_and_coordinator_nodes_does_not_restart_master_or_data_nodes() -> None:
    initial_master_task_ids = sdk_tasks.get_task_ids(service_name, "master")
    initial_data_task_ids = sdk_tasks.get_task_ids(service_name, "data")

    # Get service configuration.
    _, svc_config, _ = sdk_cmd.svc_cli(package_name, service_name, "describe", parse_json=True)

    ingest_nodes_count = get_in(["ingest_nodes", "count"], svc_config)
    coordinator_nodes_count = get_in(["coordinator_nodes", "count"], svc_config)

    global current_expected_task_count

    sdk_service.update_configuration(
        package_name,
        service_name,
        {
            "ingest_nodes": {"count": ingest_nodes_count + 1},
            "coordinator_nodes": {"count": coordinator_nodes_count + 1},
        },
        current_expected_task_count,
        # As of 2018-12-14, sdk_upgrade's `wait_for_deployment` has different behavior than
        # sdk_install's (which is what we wanted here), so don't use it. Check manually afterwards
        # with `sdk_tasks.check_running`.
        wait_for_deployment=False,
    )

    # Should be running 2 tasks more.
    current_expected_task_count += 2
    sdk_tasks.check_running(service_name, current_expected_task_count)
    # Master nodes should not restart.
    sdk_tasks.check_tasks_not_updated(service_name, "master", initial_master_task_ids)
    # Data nodes should not restart.
    sdk_tasks.check_tasks_not_updated(service_name, "data", initial_data_task_ids)


@pytest.mark.incremental
@pytest.mark.recovery
@pytest.mark.sanity
def test_adding_data_node_only_restarts_masters() -> None:
    initial_master_task_ids = sdk_tasks.get_task_ids(service_name, "master")
    initial_data_task_ids = sdk_tasks.get_task_ids(service_name, "data")
    initial_coordinator_task_ids = sdk_tasks.get_task_ids(service_name, "coordinator")

    # Get service configuration.
    _, svc_config, _ = sdk_cmd.svc_cli(package_name, service_name, "describe", parse_json=True)

    data_nodes_count = get_in(["data_nodes", "count"], svc_config)

    global current_expected_task_count

    # Increase the data nodes count by 1.
    sdk_service.update_configuration(
        package_name,
        service_name,
        {"data_nodes": {"count": data_nodes_count + 1}},
        current_expected_task_count,
        # As of 2018-12-14, sdk_upgrade's `wait_for_deployment` has different behavior than
        # sdk_install's (which is what we wanted here), so don't use it. Check manually afterwards
        # with `sdk_tasks.check_running`.
        wait_for_deployment=False,
    )

    sdk_plan.wait_for_kicked_off_deployment(service_name)
    sdk_plan.wait_for_completed_deployment(service_name)

    _, new_data_pod_info, _ = sdk_cmd.svc_cli(
        package_name, service_name, "pod info data-{}".format(data_nodes_count), parse_json=True
    )

    # Get task ID for new data node task.
    new_data_task_id = get_in([0, "info", "taskId", "value"], new_data_pod_info)

    # Should be running 1 task more.
    current_expected_task_count += 1
    sdk_tasks.check_running(service_name, current_expected_task_count)
    # Master nodes should restart.
    sdk_tasks.check_tasks_updated(service_name, "master", initial_master_task_ids)
    # Data node tasks should be the initial ones plus the new one.
    sdk_tasks.check_tasks_not_updated(
        service_name, "data", initial_data_task_ids + [new_data_task_id]
    )
    # Coordinator tasks should not restart.
    sdk_tasks.check_tasks_not_updated(service_name, "coordinator", initial_coordinator_task_ids)


@pytest.mark.incremental
@pytest.mark.sanity
def test_plugin_install_via_proxy() -> None:
    try:
        _uninstall_and_kill_proxy_before_install()

        proxy_host = sdk_cmd._internal_leader_host()
        proxy_port = 8899
        _install_and_run_proxy(proxy_host, proxy_port)

        plugin_name = "analysis-ukrainian"
        plugins = "https://s3.amazonaws.com/downloads.mesosphere.io/infinity-artifacts/elastic/analysis-ukrainian-7.3.2.zip"
        _check_proxy_healthy(proxy_host, proxy_port, plugins)

        sdk_service.update_configuration(
            package_name,
            service_name,
            {
                "elasticsearch": {
                    "plugins": plugins,
                    "plugin_http_proxy_host": proxy_host,
                    "plugin_http_proxy_port": proxy_port,
                    "plugin_https_proxy_host": proxy_host,
                    "plugin_https_proxy_port": proxy_port,
                }
            },
            config.DEFAULT_TASK_COUNT,
        )

        config.check_elasticsearch_plugin_installed(
            plugin_name,
            service_name=service_name,
            expected_nodes_count=current_expected_task_count - current_non_node_task_count,
        )
        _check_proxy_was_used()

        sdk_service.update_configuration(
            package_name,
            service_name,
            {"elasticsearch": {"plugins": ""}},
            current_expected_task_count,
        )
        config.check_elasticsearch_plugin_uninstalled(plugin_name, service_name=service_name)
    finally:
        _uninstall_and_kill_proxy()


def _install_and_run_proxy(host: str, port: int) -> None:
    rc, stdout, stderr = sdk_cmd.master_ssh(
        "sudo docker run --name py_proxy --rm -d --net=host mesosphere/proxy.py:a0021cdb3ab913495b8da53c8dc1081b895f3ef2 --hostname={} --port={}".format(
            host, port
        )
    )
    assert rc == 0


def _uninstall_and_kill_proxy() -> None:
    rc, stdout, stderr = sdk_cmd.master_ssh(
        "sudo docker stop py_proxy ; sudo docker rmi mesosphere/proxy.py:a0021cdb3ab913495b8da53c8dc1081b895f3ef2"
    )
    assert rc == 0


def _uninstall_and_kill_proxy_before_install() -> None:
    sdk_cmd.master_ssh(
        "sudo docker stop py_proxy ; sudo docker rmi mesosphere/proxy.py:a0021cdb3ab913495b8da53c8dc1081b895f3ef2"
    )


def _check_proxy_healthy(host: str, port: int, uri: str) -> None:
    rc, stdout, stderr = sdk_cmd.master_ssh(
        "curl -so /dev/null -w {} --proxy {}:{} {}".format("'%{http_code}'", host, port, uri)
    )
    assert rc == 0 and stdout == "200"


def _check_proxy_was_used() -> None:
    rc, stdout, stderr = sdk_cmd.master_ssh(
        "sudo docker logs py_proxy 2>&1 | grep 's3.amazonaws.com'"
    )
    assert rc == 0 and "s3.amazonaws.com" in stdout


@pytest.mark.incremental
@pytest.mark.sanity
def test_kibana_plugin_installation():
    try:
        elasticsearch_url = "http://" + sdk_hosts.vip_host(service_name, "coordinator", 9200)
        sdk_install.install(
            kibana_package_name,
            kibana_service_name,
            0,
            {
                "kibana": {
                    "plugins": "https://s3.amazonaws.com/downloads.mesosphere.io/infinity-artifacts/elastic/logtrail-7.3.2-0.1.31.zip",
                    "elasticsearch_url": elasticsearch_url,
                }
            },
            timeout_seconds=kibana_timeout,
            wait_for_deployment=False,
            insert_strict_options=False,
        )
        assert config.check_kibana_plugin_installed("logtrail", kibana_service_name)

    except Exception:
        log.exception(Exception)
    finally:
        log.info("Ensure elasticsearch and kibana are uninstalled.")
        sdk_install.uninstall(kibana_package_name, kibana_package_name)


@pytest.mark.incremental
@pytest.mark.sanity
def test_bootstrap_memory_lock() -> None:
    bootstrap_memory_lock_response = config._curl_query(
        service_name, "GET", "_nodes?filter_path=**.mlockall"
    )
    for (k, v) in bootstrap_memory_lock_response["nodes"].items():
        assert v["process"]["mlockall"] is True


@pytest.mark.incremental
@pytest.mark.sanity
def test_custom_log4j2_properties_base64() -> None:
    try:
        decoded_base_64_log4j2_properties = "rootLogger.level = debug"
        base_64_log4j2_properties = base64.b64encode(
            decoded_base_64_log4j2_properties.encode("utf-8")
        ).decode("utf-8")

        sdk_service.update_configuration(
            package_name,
            service_name,
            {"elasticsearch": {"custom_log4j2_properties": base_64_log4j2_properties}},
            current_expected_task_count,
        )

        cmd = "bash -c 'grep \"{}\" elasticsearch-*/config/log4j2.properties'".format(
            decoded_base_64_log4j2_properties
        )
        rc, stdout, stderr = sdk_cmd.service_task_exec(service_name, "master-0-node", cmd)
        assert rc == 0 and decoded_base_64_log4j2_properties in stdout
    finally:
        sdk_service.update_configuration(
            package_name,
            service_name,
            {"elasticsearch": {"custom_log4j2_properties": ""}},
            current_expected_task_count,
        )
