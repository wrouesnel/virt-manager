# Copyright (C) 2006-2008, 2013-2014 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import libvirt

from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf

from virtinst import log
from virtinst import xmlutil

from . import vmmenu
from .lib import uiutil
from .baseclass import vmmGObjectUI
from .connmanager import vmmConnectionManager
from .engine import vmmEngine
from .lib.graphwidgets import CellRendererSparkline

# Number of data points for performance graphs
GRAPH_LEN = 40

# fields in the tree model data set
(
    ROW_HANDLE,
    ROW_SORT_KEY,
    ROW_MARKUP,
    ROW_STATUS_ICON,
    ROW_HINT,
    ROW_IS_CONN,
    ROW_IS_CONN_CONNECTED,
    ROW_IS_VM,
    ROW_IS_VM_RUNNING,
    ROW_COLOR,
    ROW_INSPECTION_OS_ICON,
    ROW_IS_GROUP,
    ROW_GROUP_KEY,
) = range(13)

# Columns in the tree view
(COL_NAME, COL_GUEST_CPU, COL_HOST_CPU, COL_MEM, COL_DISK, COL_NETWORK,
 COL_IP_ADDR, COL_VCPUS, COL_MEM_SIZE, COL_DISK_SIZE) = range(10)


def _style_get_prop(widget, propname):
    value = GObject.Value()
    value.init(GObject.TYPE_INT)
    widget.style_get_property(propname, value)
    return value.get_int()


def _cmp(a, b):
    return (a > b) - (a < b)


def _get_inspection_icon_pixbuf(vm, w, h):
    # libguestfs gives us the PNG data as a string.
    png_data = vm.inspection.icon
    if png_data is None:
        return None

    try:
        pb = GdkPixbuf.PixbufLoader()
        pb.set_size(w, h)
        pb.write(png_data)
        pb.close()
        return pb.get_pixbuf()
    except Exception:  # pragma: no cover
        log.exception("Error loading inspection icon data")
        vm.inspection.icon = None
        return None


class vmmManager(vmmGObjectUI):
    @classmethod
    def get_instance(cls, parentobj):
        try:
            if not cls._instance:
                cls._instance = vmmManager()
            return cls._instance
        except Exception as e:  # pragma: no cover
            if not parentobj:
                raise
            parentobj.err.show_err(_("Error launching manager: %s") % str(e))

    def __init__(self):
        vmmGObjectUI.__init__(self, "manager.ui", "vmm-manager")
        self._cleanup_on_app_close()

        w, h = self.config.get_manager_window_size()
        self.topwin.set_default_size(w or 550, h or 550)
        self.prev_position = None
        self._window_size = None

        self.vmmenu = vmmenu.VMActionMenu(self, self.current_vm)
        self.shutdownmenu = vmmenu.VMShutdownMenu(self, self.current_vm)
        self.connmenu = Gtk.Menu()
        self.connmenu.get_accessible().set_name("conn-menu")
        self.connmenu_items = {}

        self.builder.connect_signals(
            {
                "on_menu_view_guest_cpu_usage_activate": self.toggle_stats_visible_guest_cpu,
                "on_menu_view_host_cpu_usage_activate": self.toggle_stats_visible_host_cpu,
                "on_menu_view_memory_usage_activate": self.toggle_stats_visible_memory_usage,
                "on_menu_view_disk_io_activate": self.toggle_stats_visible_disk,
                "on_menu_view_network_traffic_activate": self.toggle_stats_visible_network,
                "on_menu_view_ip_addresses_activate": self.toggle_stats_visible_ip_addresses,
                "on_menu_view_vcpus_activate": self.toggle_stats_visible_vcpus,
                "on_menu_view_memory_size_activate": self.toggle_stats_visible_memory_size,
                "on_menu_view_disk_size_activate": self.toggle_stats_visible_disk_size,
                "on_menu_view_group_by_state_activate": self.toggle_group_by_state,
                "on_menu_view_group_by_network_activate": self.toggle_group_by_network,
                "on_vm_manager_delete_event": self.close,
                "on_vmm_manager_configure_event": self.window_resized,
                "on_menu_file_add_connection_activate": self.open_newconn,
                "on_menu_new_vm_activate": self.new_vm,
                "on_menu_file_quit_activate": self.exit_app,
                "on_menu_file_close_activate": self.close,
                "on_vmm_close_clicked": self.close,
                "on_vm_open_clicked": self.show_vm,
                "on_vm_run_clicked": self.start_vm,
                "on_vm_new_clicked": self.new_vm,
                "on_vm_shutdown_clicked": self.poweroff_vm,
                "on_vm_pause_clicked": self.pause_vm_button,
                "on_menu_edit_details_activate": self.show_vm,
                "on_menu_edit_delete_activate": self.do_delete,
                "on_menu_host_details_activate": self.show_host,
                "on_vm_list_row_activated": self.row_activated,
                "on_vm_list_button_press_event": self.popup_vm_menu_button,
                "on_vm_list_key_press_event": self.popup_vm_menu_key,
                "on_menu_edit_preferences_activate": self.show_preferences,
                "on_menu_help_about_activate": self.show_about,
            }
        )

        # There seem to be ref counting issues with calling
        # list.get_column, so avoid it
        self.diskcol = None
        self.netcol = None
        self.memcol = None
        self.guestcpucol = None
        self.hostcpucol = None
        self.ipaddrcol = None
        self.vcpuscol = None
        self.memsizecol = None
        self.disksizecol = None
        self.spacer_txt = None
        self.init_vmlist()

        self.init_stats()
        self.init_toolbar()
        self.init_context_menus()

        self.update_current_selection()
        self.widget("vm-list").get_selection().connect("changed", self.update_current_selection)

        self.max_disk_rate = 10.0
        self.max_net_rate = 10.0

        # Initialize stat polling columns based on global polling
        # preferences (we want signal handlers for this)
        self._config_polling_change_cb(COL_GUEST_CPU)
        self._config_polling_change_cb(COL_DISK)
        self._config_polling_change_cb(COL_NETWORK)
        self._config_polling_change_cb(COL_MEM)

        connmanager = vmmConnectionManager.get_instance()
        connmanager.connect("conn-added", self._conn_added)
        connmanager.connect("conn-removed", self._conn_removed)
        for conn in connmanager.conns.values():
            self._conn_added(connmanager, conn)

    ##################
    # Common methods #
    ##################

    def show(self):
        vis = self.is_visible()
        self.topwin.present()
        if vis:
            return

        log.debug("Showing manager")
        if self.prev_position:
            self.topwin.move(*self.prev_position)
            self.prev_position = None

        vmmEngine.get_instance().increment_window_counter()

    def close(self, src_ignore=None, src2_ignore=None):
        if not self.is_visible():
            return

        log.debug("Closing manager")
        self.prev_position = self.topwin.get_position()
        self.topwin.hide()
        vmmEngine.get_instance().decrement_window_counter()

        return 1

    def _cleanup(self):
        self.diskcol = None
        self.guestcpucol = None
        self.memcol = None
        self.hostcpucol = None
        self.netcol = None
        self.ipaddrcol = None
        self.vcpuscol = None
        self.memsizecol = None
        self.disksizecol = None

        self.shutdownmenu.destroy()
        self.shutdownmenu = None
        self.vmmenu.destroy()
        self.vmmenu = None
        self.connmenu.destroy()
        self.connmenu = None
        self.connmenu_items = None

        if self._window_size:
            self.config.set_manager_window_size(*self._window_size)

    def set_startup_error(self, msg):
        self.widget("vm-notebook").set_current_page(1)
        self.widget("startup-error-label").set_text(msg)

    ################
    # Init methods #
    ################

    def init_stats(self):
        self.add_gsettings_handle(
            self.config.on_vmlist_guest_cpu_usage_visible_changed(
                self.toggle_guest_cpu_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_host_cpu_usage_visible_changed(
                self.toggle_host_cpu_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_memory_usage_visible_changed(
                self.toggle_memory_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_disk_io_visible_changed(self.toggle_disk_io_visible_widget)
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_network_traffic_visible_changed(
                self.toggle_network_traffic_visible_widget
            )
        )

        # Register callbacks with the global stats enable/disable values
        # that disable the associated vmlist widgets if reporting is disabled
        self.add_gsettings_handle(
            self.config.on_stats_enable_cpu_poll_changed(
                self._config_polling_change_cb, COL_GUEST_CPU
            )
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_disk_poll_changed(self._config_polling_change_cb, COL_DISK)
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_net_poll_changed(
                self._config_polling_change_cb, COL_NETWORK
            )
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_memory_poll_changed(self._config_polling_change_cb, COL_MEM)
        )

        self.add_gsettings_handle(
            self.config.on_vmlist_ip_addresses_visible_changed(
                self.toggle_ip_addresses_visible_widget))
        self.add_gsettings_handle(
            self.config.on_vmlist_vcpus_visible_changed(
                self.toggle_vcpus_visible_widget))
        self.add_gsettings_handle(
            self.config.on_vmlist_memory_size_visible_changed(
                self.toggle_memory_size_visible_widget))
        self.add_gsettings_handle(
            self.config.on_vmlist_disk_size_visible_changed(
                self.toggle_disk_size_visible_widget))

        self.add_gsettings_handle(
            self.config.on_vmlist_group_by_state_changed(
                self._group_by_changed_cb))
        self.add_gsettings_handle(
            self.config.on_vmlist_group_by_network_changed(
                self._group_by_changed_cb))

        self.toggle_guest_cpu_usage_visible_widget()
        self.toggle_host_cpu_usage_visible_widget()
        self.toggle_memory_usage_visible_widget()
        self.toggle_disk_io_visible_widget()
        self.toggle_network_traffic_visible_widget()
        self.toggle_ip_addresses_visible_widget()
        self.toggle_vcpus_visible_widget()
        self.toggle_memory_size_visible_widget()
        self.toggle_disk_size_visible_widget()

        self.widget("menu_view_group_by_state").set_active(
            self.config.get_vmlist_group_by_state())
        self.widget("menu_view_group_by_network").set_active(
            self.config.get_vmlist_group_by_network())

    def init_toolbar(self):
        self.widget("vm-new").set_icon_name("vm_new")
        self.widget("vm-open").set_icon_name("icon_console")

        self.widget("vm-shutdown").set_icon_name("system-shutdown")
        self.widget("vm-shutdown").set_menu(self.shutdownmenu)

        tool = self.widget("vm-toolbar")
        tool.set_property("icon-size", Gtk.IconSize.LARGE_TOOLBAR)
        for c in tool.get_children():
            c.set_homogeneous(False)

    def init_context_menus(self):
        def add_to_menu(idx, text, cb):
            item = Gtk.MenuItem.new_with_mnemonic(text)
            if cb:
                item.connect("activate", cb)
            item.get_accessible().set_name("conn-%s" % idx)
            self.connmenu.add(item)
            self.connmenu_items[idx] = item

        # Build connection context menu
        add_to_menu("create", _("_New"), self.new_vm)
        add_to_menu("connect", _("_Connect"), self.open_conn)
        add_to_menu("disconnect", _("Dis_connect"), self.close_conn)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_menu("delete", _("De_lete"), self.do_delete)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_menu("details", _("_Details"), self.show_host)
        self.connmenu.show_all()

    def init_vmlist(self):
        vmlist = self.widget("vm-list")
        self.widget("vm-notebook").set_show_tabs(False)

        rowtypes = []
        rowtypes.insert(ROW_HANDLE, object)  # backing object
        rowtypes.insert(ROW_SORT_KEY, str)  # object name
        rowtypes.insert(ROW_MARKUP, str)  # row markup text
        rowtypes.insert(ROW_STATUS_ICON, str)  # status icon name
        rowtypes.insert(ROW_HINT, str)  # row tooltip
        rowtypes.insert(ROW_IS_CONN, bool)  # if object is a connection
        rowtypes.insert(ROW_IS_CONN_CONNECTED, bool)  # if conn is connected
        rowtypes.insert(ROW_IS_VM, bool)  # if row is VM
        rowtypes.insert(ROW_IS_VM_RUNNING, bool)  # if VM is running
        rowtypes.insert(ROW_COLOR, str)  # row markup color string
        rowtypes.insert(ROW_INSPECTION_OS_ICON, GdkPixbuf.Pixbuf)  # OS icon
        rowtypes.insert(ROW_IS_GROUP, bool)  # if row is a group header
        rowtypes.insert(ROW_GROUP_KEY, str)  # group key for sorting

        model = Gtk.TreeStore(*rowtypes)
        vmlist.set_model(model)
        vmlist.set_tooltip_column(ROW_HINT)
        vmlist.set_headers_visible(True)
        vmlist.set_level_indentation(-(_style_get_prop(vmlist, "expander-size") + 3))

        nameCol = Gtk.TreeViewColumn(_("Name"))
        nameCol.set_expand(True)
        nameCol.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        nameCol.set_spacing(6)
        nameCol.set_sort_column_id(COL_NAME)
        nameCol.set_reorderable(True)

        vmlist.append_column(nameCol)

        status_icon = Gtk.CellRendererPixbuf()
        status_icon.set_property("stock-size", Gtk.IconSize.DND)
        nameCol.pack_start(status_icon, False)
        nameCol.add_attribute(status_icon, "icon-name", ROW_STATUS_ICON)
        nameCol.add_attribute(status_icon, "visible", ROW_IS_VM)

        inspection_os_icon = Gtk.CellRendererPixbuf()
        nameCol.pack_start(inspection_os_icon, False)
        nameCol.add_attribute(inspection_os_icon, "pixbuf", ROW_INSPECTION_OS_ICON)
        nameCol.add_attribute(inspection_os_icon, "visible", ROW_IS_VM)

        name_txt = Gtk.CellRendererText()
        nameCol.pack_start(name_txt, True)
        nameCol.add_attribute(name_txt, "markup", ROW_MARKUP)
        nameCol.add_attribute(name_txt, "foreground", ROW_COLOR)

        self.spacer_txt = Gtk.CellRendererText()
        self.spacer_txt.set_property("ypad", 4)
        self.spacer_txt.set_property("visible", False)
        nameCol.pack_end(self.spacer_txt, False)

        def make_stats_column(title, colnum):
            col = Gtk.TreeViewColumn(title)
            col.set_min_width(140)
            col.set_resizable(True)
            col.set_reorderable(True)

            txt = Gtk.CellRendererText()
            txt.set_property("ypad", 4)
            col.pack_start(txt, True)
            col.add_attribute(txt, "visible", ROW_IS_CONN)

            img = CellRendererSparkline()
            img.set_property("xpad", 6)
            img.set_property("ypad", 12)
            img.set_property("reversed", True)
            col.pack_start(img, True)
            col.add_attribute(img, "visible", ROW_IS_VM)

            col.set_sort_column_id(colnum)
            vmlist.append_column(col)
            return col

        def make_text_column(title, colnum, min_width=80):
            col = Gtk.TreeViewColumn(title)
            col.set_min_width(min_width)
            col.set_resizable(True)
            col.set_reorderable(True)

            conn_txt = Gtk.CellRendererText()
            conn_txt.set_property("ypad", 4)
            col.pack_start(conn_txt, True)
            col.add_attribute(conn_txt, "visible", ROW_IS_CONN)

            vm_txt = Gtk.CellRendererText()
            vm_txt.set_property("ypad", 4)
            vm_txt.set_property("xpad", 6)
            col.pack_start(vm_txt, True)
            col.add_attribute(vm_txt, "visible", ROW_IS_VM)

            col.set_sort_column_id(colnum)
            vmlist.append_column(col)
            return col

        self.ipaddrcol = make_text_column(_("IP Addresses"), COL_IP_ADDR)
        self.vcpuscol = make_text_column(_("vCPUs"), COL_VCPUS, min_width=50)
        self.memsizecol = make_text_column(_("Memory"), COL_MEM_SIZE)
        self.disksizecol = make_text_column(_("Disks"), COL_DISK_SIZE)

        self.guestcpucol = make_stats_column(_("CPU usage"), COL_GUEST_CPU)
        self.hostcpucol = make_stats_column(_("Host CPU usage"), COL_HOST_CPU)
        self.memcol = make_stats_column(_("Memory usage"), COL_MEM)
        self.diskcol = make_stats_column(_("Disk I/O"), COL_DISK)
        self.netcol = make_stats_column(_("Network I/O"), COL_NETWORK)

        model.set_sort_func(COL_NAME, self.vmlist_name_sorter)
        model.set_sort_func(COL_GUEST_CPU, self.vmlist_guest_cpu_usage_sorter)
        model.set_sort_func(COL_HOST_CPU, self.vmlist_host_cpu_usage_sorter)
        model.set_sort_func(COL_MEM, self.vmlist_memory_usage_sorter)
        model.set_sort_func(COL_DISK, self.vmlist_disk_io_sorter)
        model.set_sort_func(COL_NETWORK, self.vmlist_network_usage_sorter)
        model.set_sort_func(COL_IP_ADDR, self.vmlist_ip_addr_sorter)
        model.set_sort_func(COL_VCPUS, self.vmlist_vcpus_sorter)
        model.set_sort_func(COL_MEM_SIZE, self.vmlist_memory_size_sorter)
        model.set_sort_func(COL_DISK_SIZE, self.vmlist_disk_size_sorter)
        model.set_sort_column_id(COL_NAME, Gtk.SortType.ASCENDING)

    ##################
    # Helper methods #
    ##################

    @property
    def model(self):
        return self.widget("vm-list").get_model()

    def current_row(self):
        return uiutil.get_list_selected_row(self.widget("vm-list"))

    def current_vm(self):
        row = self.current_row()
        if not row or row[ROW_IS_CONN] or row[ROW_IS_GROUP]:
            return None

        return row[ROW_HANDLE]

    def current_conn(self):
        row = self.current_row()
        if not row:
            return None
        if row[ROW_IS_GROUP]:
            parent_iter = self.model.iter_parent(row.iter)
            while parent_iter and self.model[parent_iter][ROW_IS_GROUP]:
                parent_iter = self.model.iter_parent(parent_iter)
            if parent_iter and self.model[parent_iter][ROW_IS_CONN]:
                return self.model[parent_iter][ROW_HANDLE]
            return None
        handle = row[ROW_HANDLE]
        if row[ROW_IS_CONN]:
            return handle
        return handle.conn

    def get_row(self, conn_or_vm):
        def _walk(model, rowiter, obj):
            while rowiter:
                row = model[rowiter]
                if row[ROW_HANDLE] == obj:
                    return row
                if model.iter_has_child(rowiter):
                    ret = _walk(model, model.iter_nth_child(rowiter, 0), obj)
                    if ret:
                        return ret
                rowiter = model.iter_next(rowiter)

        if not len(self.model):
            return None
        return _walk(self.model, self.model.get_iter_first(), conn_or_vm)

    ####################
    # Action listeners #
    ####################

    def window_resized(self, ignore, ignore2):
        if not self.is_visible():
            return
        self._window_size = self.topwin.get_size()

    def exit_app(self, src_ignore=None, src2_ignore=None):
        vmmEngine.get_instance().exit_app()

    def open_newconn(self, _src):
        from .createconn import vmmCreateConn

        vmmCreateConn.get_instance(self).show(self.topwin)

    def new_vm(self, _src):
        from .createvm import vmmCreateVM

        conn = self.current_conn()
        vmmCreateVM.show_instance(self, conn and conn.get_uri() or None)

    def show_about(self, _src):
        from .about import vmmAbout

        vmmAbout.show_instance(self)

    def show_preferences(self, src_ignore):
        from .preferences import vmmPreferences

        vmmPreferences.show_instance(self)

    def show_host(self, _src):
        from .host import vmmHost

        conn = self.current_conn()
        vmmHost.show_instance(self, conn)

    def show_vm(self, _src):
        vmmenu.VMActionUI.show(self, self.current_vm())

    def row_activated(self, _src, *args):
        ignore = args
        conn = self.current_conn()
        vm = self.current_vm()
        if conn is None:
            return  # pragma: no cover

        if vm:
            self.show_vm(_src)
        elif conn.is_disconnected():
            self.open_conn()
        else:
            self.show_host(_src)

    def do_delete(self, ignore=None):
        conn = self.current_conn()
        vm = self.current_vm()
        if vm is None:
            self._do_delete_conn(conn)
        else:
            vmmenu.VMActionUI.delete(self, vm)

    def _do_delete_conn(self, conn):
        result = self.err.yes_no(
            _("This will remove the connection:\n\n%s\n\nAre you sure?") % conn.get_uri()
        )
        if not result:
            return

        vmmConnectionManager.get_instance().remove_conn(conn.get_uri())

    def set_pause_state(self, state):
        src = self.widget("vm-pause")
        try:
            src.handler_block_by_func(self.pause_vm_button)
            src.set_active(state)
        finally:
            src.handler_unblock_by_func(self.pause_vm_button)

    def pause_vm_button(self, src):
        do_pause = src.get_active()

        # Set button state back to original value: just let the status
        # update function fix things for us
        self.set_pause_state(not do_pause)

        if do_pause:
            vmmenu.VMActionUI.suspend(self, self.current_vm())
        else:
            vmmenu.VMActionUI.resume(self, self.current_vm())

    def start_vm(self, ignore):
        vmmenu.VMActionUI.run(self, self.current_vm())

    def poweroff_vm(self, _src):
        vmmenu.VMActionUI.shutdown(self, self.current_vm())

    def close_conn(self, ignore):
        conn = self.current_conn()
        if not conn.is_disconnected():
            conn.close()

    def open_conn(self, ignore=None):
        conn = self.current_conn()
        if conn.is_disconnected():
            conn.connect_once("open-completed", self._conn_open_completed_cb)
            conn.open()
            return True

    def _conn_open_completed_cb(self, _conn, ConnectError):
        if ConnectError:
            msg, details, title = ConnectError
            self.err.show_err(msg, details, title)

    ####################################
    # VM add/remove management methods #
    ####################################

    def _group_by_state(self):
        return self.config.get_vmlist_group_by_state()

    def _group_by_network(self):
        return self.config.get_vmlist_group_by_network()

    def _build_group_row(self, label, group_key):
        row = []
        row.insert(ROW_HANDLE, None)
        row.insert(ROW_SORT_KEY, group_key)
        markup = "<span weight='bold'>%s</span>" % xmlutil.xml_escape(label)
        row.insert(ROW_MARKUP, markup)
        row.insert(ROW_STATUS_ICON, None)
        row.insert(ROW_HINT, "")
        row.insert(ROW_IS_CONN, False)
        row.insert(ROW_IS_CONN_CONNECTED, False)
        row.insert(ROW_IS_VM, False)
        row.insert(ROW_IS_VM_RUNNING, False)
        row.insert(ROW_COLOR, None)
        row.insert(ROW_INSPECTION_OS_ICON, None)
        row.insert(ROW_IS_GROUP, True)
        row.insert(ROW_GROUP_KEY, group_key)
        return row

    def _vm_state_key(self, vm):
        return "0-running" if vm.is_active() else "1-inactive"

    def _vm_state_label(self, vm):
        return _("Running") if vm.is_active() else _("Inactive")

    def _vm_network_keys(self, vm):
        nets = set()
        try:
            for iface in vm.get_xmlobj(refresh_if_nec=False).devices.interface:
                src = iface.source
                if src:
                    nets.add(src)
        except Exception:
            pass
        return sorted(nets) if nets else ["(no network)"]

    def _find_or_create_group(self, parent_iter, group_key, label):
        n = self.model.iter_n_children(parent_iter)
        for i in range(n):
            child = self.model.iter_nth_child(parent_iter, i)
            if (self.model[child][ROW_IS_GROUP] and
                    self.model[child][ROW_GROUP_KEY] == group_key):
                return child
        row = self._build_group_row(label, group_key)
        new_iter = self.model.append(parent_iter, row)
        return new_iter

    def _get_vm_parent(self, conn_iter, vm):
        parent = conn_iter
        if self._group_by_state():
            state_key = self._vm_state_key(vm)
            state_label = self._vm_state_label(vm)
            parent = self._find_or_create_group(parent, state_key, state_label)
        if self._group_by_network():
            nets = self._vm_network_keys(vm)
            net_label = nets[0]
            parent = self._find_or_create_group(parent, net_label, net_label)
        return parent

    def _cleanup_empty_groups(self, parent_iter):
        i = 0
        while i < self.model.iter_n_children(parent_iter):
            child = self.model.iter_nth_child(parent_iter, i)
            if self.model[child][ROW_IS_GROUP]:
                self._cleanup_empty_groups(child)
                if self.model.iter_n_children(child) == 0:
                    self.model.remove(child)
                    continue
            i += 1

    def vm_added(self, conn, vm):
        vm_row = self._build_row(None, vm)
        conn_row = self.get_row(conn)
        parent = self._get_vm_parent(conn_row.iter, vm)
        self.model.append(parent, vm_row)

        vm.connect("state-changed", self.vm_changed)
        vm.connect("resources-sampled", self.vm_row_updated)
        vm.connect("inspection-changed", self.vm_inspection_changed)

        self.widget("vm-list").expand_row(conn_row.path, False)
        parent_path = self.model.get_path(parent)
        self.widget("vm-list").expand_row(parent_path, False)

    def vm_removed(self, conn, vm):
        row = self.get_row(vm)
        if row:
            self.model.remove(row.iter)
        conn_row = self.get_row(conn)
        if conn_row:
            self._cleanup_empty_groups(conn_row.iter)

    def _build_conn_hint(self, conn):
        hint = conn.get_uri()
        if conn.is_disconnected():
            hint = _("%(uri)s (Double click to connect)") % {"uri": conn.get_uri()}
        return hint

    def _build_conn_markup(self, conn, name):
        name = xmlutil.xml_escape(name)
        text = name
        if conn.is_disconnected():
            text = _("%(connection)s - Not Connected") % {"connection": name}
        elif conn.is_connecting():
            text = _("%(connection)s - Connecting...") % {"connection": name}

        markup = "<span size='smaller'>%s</span>" % text
        return markup

    def _build_conn_color(self, conn):
        color = None
        if conn.is_disconnected():
            color = self.config.color_insensitive
        return color

    def _build_vm_markup(self, name, status):
        domtext = "<span size='smaller' weight='bold'>%s</span>" % xmlutil.xml_escape(name)
        statetext = "<span size='smaller'>%s</span>" % status
        return domtext + "\n" + statetext

    def _build_row(self, conn, vm):
        if conn:
            name = conn.get_pretty_desc()
            markup = self._build_conn_markup(conn, name)
            status = "<span size='smaller'>%s</span>" % conn.get_state_text()
            status_icon = None
            hint = self._build_conn_hint(conn)
            color = self._build_conn_color(conn)
            os_icon = None
        else:
            name = vm.get_name_or_title()
            status = vm.run_status()
            markup = self._build_vm_markup(name, status)
            status_icon = vm.run_status_icon_name()
            hint = vm.get_description()
            color = None
            os_icon = _get_inspection_icon_pixbuf(vm, 16, 16)

        row = []
        row.insert(ROW_HANDLE, conn or vm)
        row.insert(ROW_SORT_KEY, name)
        row.insert(ROW_MARKUP, markup)
        row.insert(ROW_STATUS_ICON, status_icon)
        row.insert(ROW_HINT, xmlutil.xml_escape(hint))
        row.insert(ROW_IS_CONN, bool(conn))
        row.insert(ROW_IS_CONN_CONNECTED, bool(conn) and not conn.is_disconnected())
        row.insert(ROW_IS_VM, bool(vm))
        row.insert(ROW_IS_VM_RUNNING, bool(vm) and vm.is_active())
        row.insert(ROW_COLOR, color)
        row.insert(ROW_INSPECTION_OS_ICON, os_icon)
        row.insert(ROW_IS_GROUP, False)
        row.insert(ROW_GROUP_KEY, "")

        return row

    def _conn_added(self, _src, conn):
        # Make sure error page isn't showing
        self.widget("vm-notebook").set_current_page(0)
        if self.get_row(conn):
            return  # pragma: no cover

        conn_row = self._build_row(conn, None)
        self.model.append(None, conn_row)

        conn.connect("vm-added", self.vm_added)
        conn.connect("vm-removed", self.vm_removed)
        conn.connect("resources-sampled", self.conn_row_updated)
        conn.connect("state-changed", self.conn_state_changed)

        for vm in conn.list_vms():
            self.vm_added(conn, vm)

    def _remove_child_rows(self, row):
        child = self.model.iter_children(row.iter)
        while child is not None:  # pragma: no cover
            # vm-removed signals should handle this, this is a fallback
            # in case something goes wrong
            self.model.remove(child)
            child = self.model.iter_children(row.iter)

    def _conn_removed(self, _src, uri):
        conn_row = None
        for row in self.model:
            if row[ROW_IS_CONN] and row[ROW_HANDLE].get_uri() == uri:
                conn_row = row
                break
        if conn_row is None:  # pragma: no cover
            return

        self._remove_child_rows(conn_row)
        self.model.remove(conn_row.iter)

    #############################
    # State/UI updating methods #
    #############################

    def vm_row_updated(self, vm):
        row = self.get_row(vm)
        if row is None:  # pragma: no cover
            return
        self._refresh_vm_cache(vm)
        self.model.row_changed(row.path, row.iter)

    def _maybe_regroup_vm(self, vm):
        if not self._group_by_state() and not self._group_by_network():
            return
        row = self.get_row(vm)
        if row is None:
            return
        parent_iter = self.model.iter_parent(row.iter)
        if parent_iter is None:
            return
        conn_iter = parent_iter
        while self.model[conn_iter][ROW_IS_GROUP]:
            conn_iter = self.model.iter_parent(conn_iter)
            if conn_iter is None:
                return
        correct_parent = self._get_vm_parent(conn_iter, vm)
        if self.model.get_path(parent_iter) != self.model.get_path(correct_parent):
            vm_data = list(self.model[row.iter])
            self.model.remove(row.iter)
            self._cleanup_empty_groups(conn_iter)
            self.model.append(correct_parent, vm_data)
            path = self.model.get_path(correct_parent)
            self.widget("vm-list").expand_row(path, False)

    def vm_changed(self, vm):
        row = self.get_row(vm)
        if row is None:
            return  # pragma: no cover

        try:
            if vm == self.current_vm():
                self.update_current_selection()

            name = vm.get_name_or_title()
            status = vm.run_status()

            row[ROW_SORT_KEY] = name
            row[ROW_STATUS_ICON] = vm.run_status_icon_name()
            row[ROW_IS_VM_RUNNING] = vm.is_active()
            row[ROW_MARKUP] = self._build_vm_markup(name, status)

            desc = vm.get_description()
            row[ROW_HINT] = xmlutil.xml_escape(desc)
        except Exception as e:  # pragma: no cover
            if vm.conn.support.is_libvirt_error_no_domain(e):
                return
            raise

        self._maybe_regroup_vm(vm)
        self.vm_row_updated(vm)

    def vm_inspection_changed(self, vm):
        row = self.get_row(vm)
        if row is None:
            return  # pragma: no cover

        new_icon = _get_inspection_icon_pixbuf(vm, 16, 16)
        row[ROW_INSPECTION_OS_ICON] = new_icon

        self.vm_row_updated(vm)

    def set_initial_selection(self, uri):
        """
        Select the passed URI in the UI. Called from engine.py via
        cli --connect $URI
        """
        sel = self.widget("vm-list").get_selection()
        for row in self.model:
            if not row[ROW_IS_CONN]:
                continue  # pragma: no cover
            conn = row[ROW_HANDLE]

            if conn.get_uri() == uri:
                sel.select_iter(row.iter)
                return

    def conn_state_changed(self, conn):
        row = self.get_row(conn)
        row[ROW_SORT_KEY] = conn.get_pretty_desc()
        row[ROW_MARKUP] = self._build_conn_markup(conn, row[ROW_SORT_KEY])
        row[ROW_IS_CONN_CONNECTED] = not conn.is_disconnected()
        row[ROW_COLOR] = self._build_conn_color(conn)
        row[ROW_HINT] = self._build_conn_hint(conn)

        if not conn.is_active():
            self._remove_child_rows(row)

        self.conn_row_updated(conn)
        self.update_current_selection()

    def conn_row_updated(self, conn):
        row = self.get_row(conn)

        self.max_disk_rate = max(self.max_disk_rate, conn.disk_io_max_rate())
        self.max_net_rate = max(self.max_net_rate, conn.network_traffic_max_rate())

        self.model.row_changed(row.path, row.iter)

    def change_run_text(self, can_restore):
        if can_restore:
            text = _("_Restore")
        else:
            text = _("_Run")
        strip_text = text.replace("_", "")

        self.vmmenu.change_run_text(text)
        self.widget("vm-run").set_label(strip_text)

    def update_current_selection(self, ignore=None):
        vm = self.current_vm()
        conn = self.current_conn()

        show_open = bool(vm)
        show_details = bool(vm)
        host_details = bool(vm or conn)
        can_delete = bool(vm or conn)

        show_run = bool(vm and vm.is_runable())
        is_paused = bool(vm and vm.is_paused())
        if is_paused:
            show_pause = bool(vm and vm.is_unpauseable())
        else:
            show_pause = bool(vm and vm.is_pauseable())
        show_shutdown = bool(vm and vm.is_stoppable())

        if vm and vm.managedsave_supported:
            self.change_run_text(vm.has_managed_save())

        self.widget("vm-open").set_sensitive(show_open)
        self.widget("vm-run").set_sensitive(show_run)
        self.widget("vm-shutdown").set_sensitive(show_shutdown)
        self.widget("vm-shutdown").get_menu().update_widget_states(vm)

        self.set_pause_state(is_paused)
        self.widget("vm-pause").set_sensitive(show_pause)

        if is_paused:
            pauseTooltip = _("Resume the virtual machine")
        else:
            pauseTooltip = _("Pause the virtual machine")
        self.widget("vm-pause").set_tooltip_text(pauseTooltip)

        self.widget("menu_edit_delete").set_sensitive(can_delete)
        self.widget("menu_edit_details").set_sensitive(show_details)
        self.widget("menu_host_details").set_sensitive(host_details)

    def popup_vm_menu_key(self, widget_ignore, event):
        if Gdk.keyval_name(event.keyval) != "Menu":
            return False  # pragma: no cover

        model, treeiter = self.widget("vm-list").get_selection().get_selected()
        self.popup_vm_menu(model, treeiter, event)
        return True

    def popup_vm_menu_button(self, vmlist, event):
        if event.button != 3:
            return False

        tup = vmlist.get_path_at_pos(int(event.x), int(event.y))
        if tup is None:
            return False  # pragma: no cover
        path = tup[0]

        self.popup_vm_menu(self.model, self.model.get_iter(path), event)
        return False

    def _copy_vm_ips(self, _menuitem):
        vm = self.current_vm()
        if not vm:
            return
        ips = self._get_vm_cache(vm).get("ip_list", [])
        if ips:
            clip = Gtk.Clipboard.get_default(self.topwin.get_display())
            clip.set_text("\n".join(ips), -1)

    def popup_vm_menu(self, model, _iter, event):
        if model[_iter][ROW_IS_GROUP]:
            return False
        if model.iter_parent(_iter) is not None and model[_iter][ROW_IS_VM]:
            # Popup the vm menu
            vm = model[_iter][ROW_HANDLE]
            self.vmmenu.update_widget_states(vm)

            if not hasattr(self, "_copy_ip_item"):
                self._copy_ip_sep = Gtk.SeparatorMenuItem()
                self.vmmenu.add(self._copy_ip_sep)
                self._copy_ip_item = Gtk.MenuItem.new_with_mnemonic(
                    _("Copy _IP Addresses"))
                self._copy_ip_item.connect("activate", self._copy_vm_ips)
                self.vmmenu.add(self._copy_ip_item)

            ips = self._get_vm_cache(vm).get("ip_list", [])
            has_ips = bool(ips) and vm.is_active()
            self._copy_ip_sep.set_visible(has_ips)
            self._copy_ip_item.set_visible(has_ips)
            if has_ips:
                self._copy_ip_item.show()
                self._copy_ip_sep.show()

            self.vmmenu.popup_at_pointer(event)
        else:
            # Pop up connection menu
            conn = model[_iter][ROW_HANDLE]
            disconn = conn.is_disconnected()
            conning = conn.is_connecting()

            self.connmenu_items["create"].set_sensitive(not disconn)
            self.connmenu_items["disconnect"].set_sensitive(not (disconn or conning))
            self.connmenu_items["connect"].set_sensitive(disconn)
            self.connmenu_items["delete"].set_sensitive(disconn)

            self.connmenu.popup_at_pointer(event)

    #################
    # Stats methods #
    #################

    def vmlist_name_sorter(self, model, iter1, iter2, ignore):
        key1 = str(model[iter1][ROW_SORT_KEY]).lower()
        key2 = str(model[iter2][ROW_SORT_KEY]).lower()
        return _cmp(key1, key2)

    def _sorter_fallback(self, model, iter1, iter2):
        if model[iter1][ROW_IS_GROUP] or model[iter2][ROW_IS_GROUP]:
            return _cmp(
                str(model[iter1][ROW_SORT_KEY]).lower(),
                str(model[iter2][ROW_SORT_KEY]).lower())
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]
        if obj1 is None or obj2 is None:
            return 0
        return None, obj1, obj2

    def vmlist_guest_cpu_usage_sorter(self, model, iter1, iter2, ignore):
        ret = self._sorter_fallback(model, iter1, iter2)
        if isinstance(ret, int):
            return ret
        _, obj1, obj2 = ret
        return _cmp(obj1.guest_cpu_time_percentage(), obj2.guest_cpu_time_percentage())

    def vmlist_host_cpu_usage_sorter(self, model, iter1, iter2, ignore):
        ret = self._sorter_fallback(model, iter1, iter2)
        if isinstance(ret, int):
            return ret
        _, obj1, obj2 = ret
        return _cmp(obj1.host_cpu_time_percentage(), obj2.host_cpu_time_percentage())

    def vmlist_memory_usage_sorter(self, model, iter1, iter2, ignore):
        ret = self._sorter_fallback(model, iter1, iter2)
        if isinstance(ret, int):
            return ret
        _, obj1, obj2 = ret
        return _cmp(obj1.stats_memory(), obj2.stats_memory())

    def vmlist_disk_io_sorter(self, model, iter1, iter2, ignore):
        ret = self._sorter_fallback(model, iter1, iter2)
        if isinstance(ret, int):
            return ret
        _, obj1, obj2 = ret
        return _cmp(obj1.disk_io_rate(), obj2.disk_io_rate())

    def vmlist_network_usage_sorter(self, model, iter1, iter2, ignore):
        ret = self._sorter_fallback(model, iter1, iter2)
        if isinstance(ret, int):
            return ret
        _, obj1, obj2 = ret
        return _cmp(obj1.network_traffic_rate(), obj2.network_traffic_rate())

    def _config_polling_change_cb(self, column):
        # pylint: disable=redefined-variable-type
        if column == COL_GUEST_CPU:
            widgn = ["menu_view_stats_guest_cpu", "menu_view_stats_host_cpu"]
            do_enable = self.config.get_stats_enable_cpu_poll()
        if column == COL_DISK:
            widgn = "menu_view_stats_disk"
            do_enable = self.config.get_stats_enable_disk_poll()
        elif column == COL_NETWORK:
            widgn = "menu_view_stats_network"
            do_enable = self.config.get_stats_enable_net_poll()
        elif column == COL_MEM:
            widgn = "menu_view_stats_memory"
            do_enable = self.config.get_stats_enable_memory_poll()

        for w in xmlutil.listify(widgn):
            widget = self.widget(w)
            tool_text = ""

            if do_enable:
                widget.set_sensitive(True)
            else:
                widget.set_active(False)
                widget.set_sensitive(False)
                tool_text = _("Disabled in preferences dialog.")
            widget.set_tooltip_text(tool_text)

    def _update_spacer_visibility(self):
        all_cols = [
            self.netcol, self.diskcol, self.memcol,
            self.guestcpucol, self.hostcpucol,
            self.ipaddrcol, self.vcpuscol,
            self.memsizecol, self.disksizecol,
        ]
        any_visible = any(c.get_visible() for c in all_cols)
        self.spacer_txt.set_property("visible", not any_visible)

    def _toggle_graph_helper(self, do_show, col, datafunc, menu):
        img = -1
        for child in col.get_cells():
            if isinstance(child, CellRendererSparkline):
                img = child
        datafunc = do_show and datafunc or None

        col.set_cell_data_func(img, datafunc, None)
        col.set_visible(do_show)
        self.widget(menu).set_active(do_show)
        self._update_spacer_visibility()

    def _toggle_text_helper(self, do_show, col, datafunc, menu,
                             conn_datafunc=None):
        cells = col.get_cells()
        conn_txt = cells[0]
        vm_txt = cells[-1]
        datafunc = do_show and datafunc or None
        conn_datafunc = do_show and conn_datafunc or None

        col.set_cell_data_func(vm_txt, datafunc, None)
        col.set_cell_data_func(conn_txt, conn_datafunc, None)
        col.set_visible(do_show)
        self.widget(menu).set_active(do_show)
        self._update_spacer_visibility()

    def toggle_network_traffic_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_network_traffic_visible(),
            self.netcol,
            self.network_traffic_img,
            "menu_view_stats_network",
        )

    def toggle_disk_io_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_disk_io_visible(),
            self.diskcol,
            self.disk_io_img,
            "menu_view_stats_disk",
        )

    def toggle_memory_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_memory_usage_visible(),
            self.memcol,
            self.memory_usage_img,
            "menu_view_stats_memory",
        )

    def toggle_guest_cpu_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_guest_cpu_usage_visible(),
            self.guestcpucol,
            self.guest_cpu_usage_img,
            "menu_view_stats_guest_cpu",
        )

    def toggle_host_cpu_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_host_cpu_usage_visible(),
            self.hostcpucol,
            self.host_cpu_usage_img,
            "menu_view_stats_host_cpu",
        )

    def toggle_ip_addresses_visible_widget(self):
        self._toggle_text_helper(
            self.config.is_vmlist_ip_addresses_visible(),
            self.ipaddrcol,
            self.ip_addr_data_func,
            "menu_view_stats_ip_addresses",
        )

    def toggle_vcpus_visible_widget(self):
        self._toggle_text_helper(
            self.config.is_vmlist_vcpus_visible(),
            self.vcpuscol,
            self.vcpus_data_func,
            "menu_view_stats_vcpus",
            conn_datafunc=self.conn_vcpus_data_func,
        )

    def toggle_memory_size_visible_widget(self):
        self._toggle_text_helper(
            self.config.is_vmlist_memory_size_visible(),
            self.memsizecol,
            self.memory_size_data_func,
            "menu_view_stats_memory_size",
            conn_datafunc=self.conn_memory_size_data_func,
        )

    def toggle_disk_size_visible_widget(self):
        self._toggle_text_helper(
            self.config.is_vmlist_disk_size_visible(),
            self.disksizecol,
            self.disk_size_data_func,
            "menu_view_stats_disk_size",
            conn_datafunc=self.conn_disk_size_data_func,
        )

    def toggle_stats_visible(self, src, stats_id):
        visible = src.get_active()
        set_stats = {
            COL_GUEST_CPU: self.config.set_vmlist_guest_cpu_usage_visible,
            COL_HOST_CPU: self.config.set_vmlist_host_cpu_usage_visible,
            COL_MEM: self.config.set_vmlist_memory_usage_visible,
            COL_DISK: self.config.set_vmlist_disk_io_visible,
            COL_NETWORK: self.config.set_vmlist_network_traffic_visible,
            COL_IP_ADDR: self.config.set_vmlist_ip_addresses_visible,
            COL_VCPUS: self.config.set_vmlist_vcpus_visible,
            COL_MEM_SIZE: self.config.set_vmlist_memory_size_visible,
            COL_DISK_SIZE: self.config.set_vmlist_disk_size_visible,
        }
        set_stats[stats_id](visible)

    def toggle_stats_visible_guest_cpu(self, src):
        self.toggle_stats_visible(src, COL_GUEST_CPU)

    def toggle_stats_visible_host_cpu(self, src):
        self.toggle_stats_visible(src, COL_HOST_CPU)

    def toggle_stats_visible_memory_usage(self, src):
        self.toggle_stats_visible(src, COL_MEM)

    def toggle_stats_visible_disk(self, src):
        self.toggle_stats_visible(src, COL_DISK)

    def toggle_stats_visible_network(self, src):
        self.toggle_stats_visible(src, COL_NETWORK)

    def toggle_stats_visible_ip_addresses(self, src):
        self.toggle_stats_visible(src, COL_IP_ADDR)

    def toggle_stats_visible_vcpus(self, src):
        self.toggle_stats_visible(src, COL_VCPUS)

    def toggle_stats_visible_memory_size(self, src):
        self.toggle_stats_visible(src, COL_MEM_SIZE)

    def toggle_stats_visible_disk_size(self, src):
        self.toggle_stats_visible(src, COL_DISK_SIZE)

    def toggle_group_by_state(self, src):
        self.config.set_vmlist_group_by_state(src.get_active())

    def toggle_group_by_network(self, src):
        self.config.set_vmlist_group_by_network(src.get_active())

    def _group_by_changed_cb(self, *_args):
        self.widget("menu_view_group_by_state").set_active(
            self.config.get_vmlist_group_by_state())
        self.widget("menu_view_group_by_network").set_active(
            self.config.get_vmlist_group_by_network())
        self._rebuild_groups()

    def _rebuild_groups(self):
        vmlist = self.widget("vm-list")
        for row in self.model:
            if not row[ROW_IS_CONN]:
                continue
            conn = row[ROW_HANDLE]
            conn_iter = row.iter

            vms = list(self._iter_child_vms(self.model, conn_iter))

            vm_data = []
            for vm in vms:
                vm_row = self.get_row(vm)
                if vm_row:
                    vm_data.append((vm, list(self.model[vm_row.iter])))

            child = self.model.iter_children(conn_iter)
            while child is not None:
                self.model.remove(child)
                child = self.model.iter_children(conn_iter)

            for vm, data in vm_data:
                parent = self._get_vm_parent(conn_iter, vm)
                self.model.append(parent, data)

            vmlist.expand_row(row.path, True)

    def guest_cpu_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.guest_cpu_time_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def host_cpu_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.host_cpu_time_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def memory_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.stats_memory_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def disk_io_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        d1, d2 = obj.disk_io_vectors(GRAPH_LEN, self.max_disk_rate)
        data = [(x + y) / 2 for x, y in zip(d1, d2)]
        cell.set_property("data_array", data)

    def network_traffic_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        d1, d2 = obj.network_traffic_vectors(GRAPH_LEN, self.max_net_rate)
        data = [(x + y) / 2 for x, y in zip(d1, d2)]
        cell.set_property("data_array", data)

    #########################
    # Info column data funcs #
    #########################

    _INACTIVE_COLOR = "#888888"
    _MONO = "font_family='monospace' size='smaller'"

    def _mono(self, text, color=None):
        escaped = xmlutil.xml_escape(str(text))
        if color:
            return "<span %s foreground='%s'>%s</span>" % (
                self._MONO, color, escaped)
        return "<span %s>%s</span>" % (self._MONO, escaped)

    @staticmethod
    def _fmt_disk_alloc(alloc, cap):
        cap_gb = cap / (1024 ** 3)
        alloc_gb = alloc / (1024 ** 3)
        if alloc_gb > 0 and alloc_gb < cap_gb * 0.99:
            return "%.1f/%.1f GiB" % (alloc_gb, cap_gb)
        return "%.1f GiB" % cap_gb

    def _fmt_disk_markup(self, target, alloc, cap):
        size_text = xmlutil.xml_escape(self._fmt_disk_alloc(alloc, cap))
        markup = "<span %s>%s</span>" % (self._MONO, size_text)
        if target:
            name = xmlutil.xml_escape(target)
            markup += (" <span size='smaller' weight='bold'>%s</span>"
                       % name)
        return markup

    def _get_disk_info(self, obj, disk):
        path = disk.get_source_path()
        if not path:
            return None
        target = disk.target or ""
        if obj.is_active():
            try:
                info = obj._backend.blockInfo(path)
                return (target, info[1], info[0])
            except Exception:
                pass
        try:
            vol = obj.conn.get_vol_by_path(path)
            if vol:
                xmlobj = vol.get_xmlobj()
                return (target, xmlobj.allocation or 0, xmlobj.capacity or 0)
        except Exception:
            pass
        return None

    def _refresh_vm_cache(self, obj):
        """Recompute cached markup for a VM's info columns."""
        cache = getattr(obj, "_vmlist_cache", None)
        if cache is None:
            cache = {}
            obj._vmlist_cache = cache

        active = obj.is_active()
        color = None if active else self._INACTIVE_COLOR

        # IPs
        ip_list = []
        if active:
            addrs = {}
            try:
                addrs = obj._backend.interfaceAddresses(
                    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
            except Exception:
                pass
            if not addrs:
                try:
                    arp_flag = getattr(
                        libvirt, "VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP", 3)
                    addrs = obj._backend.interfaceAddresses(arp_flag)
                except Exception:
                    pass
            for _name, info in sorted(addrs.items()):
                for addr in info.get("addrs", []):
                    if addr["type"] == 0:
                        ip_list.append(addr["addr"])
        cache["ip_list"] = ip_list
        cache["ip_markup"] = "\n".join(
            self._mono(ip) for ip in ip_list) if ip_list else ""

        # vCPUs
        xmlobj = obj.get_xmlobj(refresh_if_nec=False)
        vcpus = xmlobj.vcpus or 0
        cache["vcpus"] = vcpus
        cache["vcpus_markup"] = self._mono(vcpus, color) if vcpus else ""

        # Memory
        maxmem_mb = (xmlobj.memory or 0) // 1024
        curmem = obj.stats_memory()
        curmem_mb = curmem // 1024 if curmem else 0
        if active and curmem_mb > 0:
            mem_text = "%d / %d MiB" % (curmem_mb, maxmem_mb)
        else:
            mem_text = "%d MiB" % maxmem_mb if maxmem_mb else ""
        cache["memory"] = xmlobj.memory or 0
        cache["memory_markup"] = self._mono(mem_text, color) if mem_text else ""

        # Disks
        total_alloc = 0
        total_cap = 0
        disk_parts = []
        for disk in obj.get_disk_devices_norefresh():
            if disk.device != "disk":
                continue
            info = self._get_disk_info(obj, disk)
            if not info:
                continue
            total_alloc += info[1]
            total_cap += info[2]
            disk_parts.append(self._fmt_disk_markup(*info))
        cache["disk_alloc"] = total_alloc
        cache["disk_cap"] = total_cap
        cache["disk_markup"] = "\n".join(disk_parts)

        return cache

    def _get_vm_cache(self, obj):
        cache = getattr(obj, "_vmlist_cache", None)
        if cache is None:
            cache = self._refresh_vm_cache(obj)
        return cache

    def _iter_child_vms(self, model, parent_iter):
        n = model.iter_n_children(parent_iter)
        for i in range(n):
            child = model.iter_nth_child(parent_iter, i)
            if model[child][ROW_IS_GROUP]:
                yield from self._iter_child_vms(model, child)
            elif model[child][ROW_IS_VM]:
                obj = model[child][ROW_HANDLE]
                if obj is not None:
                    yield obj

    def ip_addr_data_func(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return
        cell.set_property("markup", self._get_vm_cache(obj).get(
            "ip_markup", ""))

    def vcpus_data_func(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return
        cell.set_property("markup", self._get_vm_cache(obj).get(
            "vcpus_markup", ""))

    def memory_size_data_func(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return
        cell.set_property("markup", self._get_vm_cache(obj).get(
            "memory_markup", ""))

    def disk_size_data_func(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return
        cell.set_property("markup", self._get_vm_cache(obj).get(
            "disk_markup", ""))

    ######################################
    # Connection row aggregate data funcs #
    ######################################

    def conn_vcpus_data_func(self, column_ignore, cell, model, _iter, data):
        if not model[_iter][ROW_IS_CONN]:
            return
        total = 0
        for obj in self._iter_child_vms(model, _iter):
            if obj.is_active():
                total += self._get_vm_cache(obj).get("vcpus", 0)
        cell.set_property("markup",
                          self._mono(total) if total else "")

    def conn_memory_size_data_func(self, column_ignore, cell, model,
                                   _iter, data):
        if not model[_iter][ROW_IS_CONN]:
            return
        total_mem = 0
        for obj in self._iter_child_vms(model, _iter):
            if obj.is_active():
                total_mem += self._get_vm_cache(obj).get("memory", 0)
        if not total_mem:
            cell.set_property("markup", "")
            return
        total_mem_mb = total_mem // 1024
        cell.set_property("markup", self._mono("%d MiB" % total_mem_mb))

    def conn_disk_size_data_func(self, column_ignore, cell, model,
                                 _iter, data):
        if not model[_iter][ROW_IS_CONN]:
            return
        total_alloc = 0
        total_cap = 0
        for obj in self._iter_child_vms(model, _iter):
            cache = self._get_vm_cache(obj)
            total_alloc += cache.get("disk_alloc", 0)
            total_cap += cache.get("disk_cap", 0)
        if not total_cap:
            cell.set_property("markup", "")
            return
        cell.set_property("markup", self._fmt_disk_markup(
            "", total_alloc, total_cap))

    ###########################
    # Info column sort funcs  #
    ###########################

    def vmlist_ip_addr_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]
        def _first_ip(obj):
            if not hasattr(obj, "conn"):
                return ""
            ips = self._get_vm_cache(obj).get("ip_list", [])
            return ips[0] if ips else ""
        return _cmp(_first_ip(obj1), _first_ip(obj2))

    def vmlist_vcpus_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]
        def _vcpus(obj):
            if not hasattr(obj, "conn"):
                return 0
            return self._get_vm_cache(obj).get("vcpus", 0)
        return _cmp(_vcpus(obj1), _vcpus(obj2))

    def vmlist_memory_size_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]
        def _mem(obj):
            if not hasattr(obj, "conn"):
                return 0
            return self._get_vm_cache(obj).get("memory", 0)
        return _cmp(_mem(obj1), _mem(obj2))

    def vmlist_disk_size_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]
        def _disk(obj):
            if not hasattr(obj, "conn"):
                return 0
            return self._get_vm_cache(obj).get("disk_cap", 0)
        return _cmp(_get_disk_total(obj1), _get_disk_total(obj2))
