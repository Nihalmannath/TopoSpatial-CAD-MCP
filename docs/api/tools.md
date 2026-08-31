# MCP Tools

The 8 unified MCP tools exposed to Claude or any MCP client. Drawing, layer,
entity, block, and file tools accept compact shorthand. `manage_session` accepts
JSON operations, while `manage_topology` uses explicit structured arguments and
strict JSON-LD-shaped change documents.

## draw_entities

::: mcp_tools.tools.drawing
    options:
      show_source: false
      members: [register_drawing_tools]
      filters: ["!^_"]

## manage_layers

::: mcp_tools.tools.layers
    options:
      show_source: false
      members: [register_layer_tools]
      filters: ["!^_"]

## manage_blocks

::: mcp_tools.tools.blocks
    options:
      show_source: false
      members: [register_block_tools]
      filters: ["!^_"]

## manage_entities

::: mcp_tools.tools.entities
    options:
      show_source: false
      members: [register_entity_tools]
      filters: ["!^_"]

## manage_files

::: mcp_tools.tools.files
    options:
      show_source: false
      members: [register_file_tools]
      filters: ["!^_"]

## manage_session

::: mcp_tools.tools.session
    options:
      show_source: false
      members: [register_session_tools]
      filters: ["!^_"]

## export_data

::: mcp_tools.tools.export
    options:
      show_source: false
      members: [register_export_tools]
      filters: ["!^_"]

## manage_topology

::: mcp_tools.tools.topology
    options:
      show_source: false
      members: [register_topology_tools]
      filters: ["!^_"]
