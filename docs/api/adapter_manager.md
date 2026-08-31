# Adapter Manager

Singleton registry that manages active CAD type detection and thread-local
adapter instances. Application/document COM proxies are not shared between MCP
or dashboard worker apartments.

::: adapters.adapter_manager
    options:
      show_source: false
      members_order: source
      show_root_heading: true
      filters: ["!^_"]
