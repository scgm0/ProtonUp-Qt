import os
import shutil
import subprocess
import json
import vdf
import requests
import threading
import pkgutil
import binascii
from steam.utils.appcache import parse_appinfo

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMessageBox, QApplication

from pupgui2.constants import APP_NAME, APP_ID, APP_ICON_FILE
from pupgui2.constants import PROTON_EAC_RUNTIME_APPID, PROTON_BATTLEYE_RUNTIME_APPID, PROTON_NEXT_APPID, STEAMLINUXRUNTIME_APPID, STEAMLINUXRUNTIME_SOLDIER_APPID, STEAMLINUXRUNTIME_SNIPER_APPID
from pupgui2.constants import LOCAL_AWACY_GAME_LIST, PROTONDB_API_URL
from pupgui2.constants import STEAM_STL_INSTALL_PATH, STEAM_STL_CONFIG_PATH, STEAM_STL_SHELL_FILES, STEAM_STL_FISH_VARIABLES, HOME_DIR, IS_FLATPAK
from pupgui2.datastructures import SteamApp, AWACYStatus, BasicCompatTool, CTType, SteamUser, RuntimeType


_cached_app_list = []
_cached_steam_ctool_id_map = None


def get_steam_vdf_compat_tool_mapping(vdf_file: dict) -> dict:

    s = vdf_file.get('InstallConfigStore', {}).get('Software', {})

    # Sometimes the key is 'Valve', sometimes 'valve', see #226
    c = s.get('Valve') or s.get('valve')
    if not c:
        print('Error! config.vdf InstallConfigStore.Software neither contains key "Valve" nor "valve" - config.vdf file may be invalid!')
        return {}

    m = c.get('Steam', {}).get('CompatToolMapping', {})

    if not m:  # equal to m == {} , may occur after fresh Steam installation
        print('Warning: CompatToolMapping is empty')

    return m


def get_steam_app_list(steam_config_folder: str, cached=False, no_shortcuts=False) -> list[SteamApp]:
    """
    Returns a list of installed Steam apps and optionally game names and the compatibility tool they are using
    steam_config_folder = e.g. '~/.steam/root/config'
    Return Type: list[SteamApp]
    """
    global _cached_app_list

    if cached and _cached_app_list != []:
        return _cached_app_list

    libraryfolders_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'libraryfolders.vdf')
    config_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'config.vdf')

    apps = []

    try:
        v = vdf_safe_load(libraryfolders_vdf_file)
        c = get_steam_vdf_compat_tool_mapping(vdf_safe_load(config_vdf_file))

        for fid in v.get('libraryfolders'):
            if 'apps' not in v.get('libraryfolders').get(fid):
                continue
            fid_path = v.get('libraryfolders').get(fid).get('path')
            fid_libraryfolder_path = fid_path
            if fid == '0':
                fid_path = os.path.join(fid_path, 'steamapps', 'common')
            for appid in v.get('libraryfolders').get(fid).get('apps'):
                # Skip if app isn't installed to `/path/to/steamapps/common` - Skips soundtracks
                fid_steamapps_path = os.path.join(fid_libraryfolder_path, 'steamapps')  # e.g. /home/gaben/Games/steamapps
                appmanifest_path = os.path.join(fid_steamapps_path, f'appmanifest_{appid}.acf')
                if os.path.isfile(appmanifest_path):
                    appmanifest_install_path = vdf_safe_load(appmanifest_path).get('AppState', {}).get('installdir', None)
                    if not appmanifest_install_path or not os.path.isdir(os.path.join(fid_steamapps_path, 'common', appmanifest_install_path)):
                        continue

                app = SteamApp()
                app.app_id = int(appid)
                app.libraryfolder_id = fid
                app.libraryfolder_path = fid_path
                app.anticheat_runtimes = { RuntimeType.EAC: False, RuntimeType.BATTLEYE: False }  # Have to initialize as False here for some reason...
                if ct := c.get(appid):
                    app.compat_tool = ct.get('name')
                apps.append(app)
        apps = update_steamapp_info(steam_config_folder, apps)
        apps = update_steamapp_awacystatus(apps)
    except Exception as e:
        print('Error (get_steam_app_list): Could not get a list of all Steam apps:', e)
    else:
        if not no_shortcuts:
            apps.extend(get_steam_shortcuts_list(steam_config_folder, c))

    _cached_app_list = apps
    return apps


def get_steam_shortcuts_list(steam_config_folder: str, compat_tools: dict=None) -> list[SteamApp]:
    """
    Returns a list of Steam shortcut apps (Non-Steam games added to the library) and the compatibility tool they are using
    steam_config_folder = e.g. '~/.steam/root/config'
    compat_tools (optional): dict, mapping the compat tools from config.vdf. Will be loaded from steam_config_folder if not specified
    Return Type: list[SteamApp]
    """
    users_folder = os.path.realpath(os.path.join(os.path.expanduser(steam_config_folder), os.pardir, 'userdata'))
    config_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'config.vdf')

    apps = []

    try:
        if not compat_tools:
            compat_tools = get_steam_vdf_compat_tool_mapping(vdf_safe_load(config_vdf_file))

        for userf in os.listdir(users_folder):
            user_directory = os.path.join(users_folder, userf)
            if not os.path.isdir(user_directory):
                continue

            shortcuts_file = os.path.join(user_directory,'config/shortcuts.vdf')
            if not os.path.exists(shortcuts_file):
                continue
        
            shortcuts_vdf = vdf.binary_load(open(shortcuts_file,'rb'))
            if 'shortcuts' not in shortcuts_vdf:
                continue

            for sid,svalue in shortcuts_vdf.get('shortcuts').items():
                app = SteamApp()
                appid = svalue.get('appid')
                if appid < 0:
                    appid = appid +(1 << 32) #convert to unsigned
                
                app.app_id = appid
                app.shortcut_id = sid
                app.shortcut_startdir = svalue.get('StartDir')
                app.shortcut_exe = svalue.get('Exe')
                app.shortcut_icon = svalue.get('icon')
                app.shortcut_user = userf
                app.app_type = 'game'
                app.game_name = svalue.get('AppName') or svalue.get('appname')
                if ct := compat_tools.get(str(appid)):
                    app.compat_tool = ct.get('name')
                apps.append(app)
    except Exception as e:
        print('Error (get_steam_shortcuts_list): Could not get a list of Steam shortcut apps:', e)
    
    return apps


def get_steam_game_list(steam_config_folder: str, compat_tool: BasicCompatTool | None=None, cached=False) -> list[SteamApp]:
    """
    Returns a list of installed Steam games and which compatibility tools they are using.
    Specify compat_tool to only return games using the specified tool.
    Return Type: list[SteamApp]
    """
    apps = get_steam_app_list(steam_config_folder, cached=cached)

    return [app for app in apps if app.app_type == 'game' and (compat_tool is None or app.compat_tool == compat_tool.get_internal_name() or ctool_is_runtime_for_app(app, compat_tool))]


def ctool_is_runtime_for_app(app: SteamApp, compat_tool: BasicCompatTool | None):
    """
    Check if a compatibility tool name corresponds to a runtime in use by a SteamApp by comparing a hardcoded name against app.anticheat_runtimes
    Example: Compatibility tool name is 'ProtonEasyAntiCheatRuntime' and the app.anticheat_runtimes has RuntimeType.EAC as True
    """
    if not compat_tool or not compat_tool.ct_type == CTType.STEAM_RT:
        return False

    compat_tool_name = compat_tool.get_internal_name().lower().replace(' ', '')
    return 'easyanticheatruntime' in compat_tool_name and app.anticheat_runtimes[RuntimeType.EAC] \
        or 'battleyeruntime' in compat_tool_name and app.anticheat_runtimes[RuntimeType.BATTLEYE]


def get_steam_ct_game_map(steam_config_folder: str, compat_tools: list[BasicCompatTool], cached=False) -> dict[BasicCompatTool, list[SteamApp]]:
    """
    Returns a dict that maps a list of Steam games to each compatibility given in the compat_tools parameter.
    Steam games without a selected compatibility tool are not included.
    Informal Example: { GE-Proton7-43: [GTA V, Cyberpunk 2077], SteamTinkerLaunch: [Vecter, Terraria] }
    Return Type: dict[BasicCompatTool, list[SteamApp]]
    """
    ct_game_map = {}

    apps = get_steam_app_list(steam_config_folder, cached=cached)

    ct_name_object_map = {ct.get_internal_name(): ct for ct in compat_tools}

    for app in apps:
        if app.app_type == 'game' and app.compat_tool in ct_name_object_map:
            ct_game_map.setdefault(ct_name_object_map.get(app.compat_tool), []).append(app)

    return ct_game_map


def get_steam_ctool_list(steam_config_folder: str, only_proton=False, cached=False) -> list[SteamApp]:
    """
    Returns a list of installed Steam compatibility tools (official tools).
    Return Type: list[SteamApp]
    """
    ctools = []
    apps = get_steam_app_list(steam_config_folder, cached=cached)

    for app in apps:
        if app.ctool_name != '':
            if only_proton and app.ctool_from_oslist != 'windows':
                continue
            ctools.append(app)

    return ctools


def get_steam_global_ctool_name(steam_config_folder: str) -> str:

    """
    Return the internal name of the global Steam compatibility tool selected in the Steam Play settings from the Steam Client.
    Return Type: str
    """

    config_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'config.vdf')
    d = get_steam_vdf_compat_tool_mapping(vdf_safe_load(config_vdf_file))

    return d.get('0', {}).get('name', '')


def get_steam_acruntime_list(steam_config_folder: str, cached=False) -> list[BasicCompatTool]:
    """
    Returns a list of installed Steam Proton anticheat(EAC/BattlEye) Runtimes.
    Return Type: list[BasicCompatTool]
    """
    runtimes = []
    apps = get_steam_app_list(steam_config_folder, cached=cached)

    for app in apps:
        if app.app_type == 'acruntime':
            ct = BasicCompatTool(app.game_name, app.libraryfolder_path, '', CTType.STEAM_RT)
            runtimes.append(ct)

    return runtimes


def _get_steam_ctool_info(steam_config_folder: str) -> dict[str, dict[str, str]]:
    """
    Returns a dict that maps the compatibility tool appid to tool info (name e.g. 'proton_7' and from_oslist)
    Return Type: dict[str, dict]
        Contents: appid str -> {'name', 'from_oslist'}
    """
    global _cached_steam_ctool_id_map

    if _cached_steam_ctool_id_map is not None:
        return _cached_steam_ctool_id_map

    appinfo_file = os.path.join(os.path.expanduser(steam_config_folder), '../appcache/appinfo.vdf')
    appinfo_file = os.path.realpath(appinfo_file)

    ctool_map = {}
    compat_tools = {}
    try:
        with open(appinfo_file, 'rb') as f:
            header, apps = parse_appinfo(f, mapper=dict)
            for steam_app in apps:
                if steam_app.get('appid') == 891390:
                    compat_tools = steam_app.get('data').get('appinfo').get('extended').get('compat_tools')
                    break
    except Exception as e:
        print('Error getting ctool map from appinfo.vdf:', e)
    else:
        for t in compat_tools:
            ctool_map[compat_tools.get(t).get('appid')] = {'name': t, 'from_oslist': compat_tools.get(t).get('from_oslist')}

    _cached_steam_ctool_id_map = ctool_map
    return ctool_map


def update_steamapp_info(steam_config_folder: str, steamapp_list: list[SteamApp]) -> list[SteamApp]:
    """
    Get Steam game names and information for provided SteamApps
    Return Type: list[SteamApp]
    """
    appinfo_file = os.path.join(os.path.expanduser(steam_config_folder), '../appcache/appinfo.vdf')
    appinfo_file = os.path.realpath(appinfo_file)
    sapps: dict[str, SteamApp] = {app.get_app_id_str(): app for app in steamapp_list}
    len_sapps = len(sapps)
    cnt = 0
    try:
        ctool_map: dict[str, dict[str, str]] = _get_steam_ctool_info(steam_config_folder)
        with open(appinfo_file, 'rb') as f:
            _, apps = parse_appinfo(f, mapper=dict)
            for steam_app in apps:
                appid_str = str(steam_app.get('appid'))
                if a := sapps.get(appid_str):
                    app_appinfo = steam_app.get('data', {}).get('appinfo', {})
                    app_appinfo_common = app_appinfo.get('common', {})

                    # Dictionary of Dictionaries with dependency info, primarily Proton anti-cheat runtimes
                    # Example: {'0': {'src_os': 'windows', 'dest_os': 'linux', 'appid': 1826330, 'comment': 'EAC runtime'}}
                    app_additional_dependencies = app_appinfo.get('extended', {}).get('additional_dependencies', {})

                    a.game_name = str(app_appinfo_common.get('name', ''))
                    a.deck_compatibility = app_appinfo_common.get('steam_deck_compatibility', {})
                    for dep in app_additional_dependencies.values():
                        a.anticheat_runtimes[RuntimeType.EAC] = dep.get('appid', -1) == PROTON_EAC_RUNTIME_APPID
                        a.anticheat_runtimes[RuntimeType.BATTLEYE] = dep.get('appid', -1) == PROTON_BATTLEYE_RUNTIME_APPID

                    # Configure app types
                    if a.app_id in [PROTON_EAC_RUNTIME_APPID, PROTON_BATTLEYE_RUNTIME_APPID]:
                        a.app_type = 'acruntime'
                    elif a.app_id in [STEAMLINUXRUNTIME_APPID, STEAMLINUXRUNTIME_SOLDIER_APPID, STEAMLINUXRUNTIME_SNIPER_APPID]:
                        a.app_type = 'runtime'
                    elif 'Steamworks' in a.game_name:
                        a.app_type = 'steamworks'
                    elif steam_app.get('appid') in ctool_map:
                        ct = ctool_map.get(steam_app.get('appid'))
                        a.ctool_name = ct.get('name')
                        a.ctool_from_oslist = ct.get('from_oslist')
                    elif a.app_id == PROTON_NEXT_APPID:  # see https://github.com/DavidoTek/ProtonUp-Qt/pull/280
                        a.app_type = 'useless-proton-next'
                    else:
                        a.app_type = 'game'
                    cnt += 1
                if cnt == len_sapps:
                    break
    except Exception as e:
        print('Error updating SteamApp info from appinfo.vdf:', e)
    return list(sapps.values())


def update_steamapp_awacystatus(steamapp_list: list[SteamApp]) -> list[SteamApp]:  # Download file in thread on start...
    """
    Set the areweanticheatyet.com for the games.
    Return Type: list[SteamApp]
    """
    if not os.path.exists(LOCAL_AWACY_GAME_LIST):
        return steamapp_list

    try:
        with open(LOCAL_AWACY_GAME_LIST, 'r') as f:
            gm = {g.get('name'): g.get('status') for g in json.load(f)}

        for app in steamapp_list:
            if app.game_name != '' and app.game_name in gm:
                status = gm[app.game_name]
                if status == 'Supported':
                    app.awacy_status = AWACYStatus.ASUPPORTED
                elif status == 'Planned':
                    app.awacy_status = AWACYStatus.PLANNED
                elif status == 'Running':
                    app.awacy_status = AWACYStatus.RUNNING
                elif status == 'Broken':
                    app.awacy_status = AWACYStatus.BROKEN
                elif status == 'Denied':
                    app.awacy_status = AWACYStatus.DENIED
    except Exception as e:
        print('Error updating the areweanticheatyet.com status:', e)
        return steamapp_list

    return steamapp_list


def get_protondb_status_thread(game: SteamApp, signal: Signal) -> None:
    """ Downloads the ProtonDB.com status and calls the Qt Signal "signal" when done. Use with "get_protondb_status"!"""
    try:
        json_url = PROTONDB_API_URL.format(game_id=str(game.app_id))
        r = requests.get(json_url)
        if r.status_code == 200:
            game.protondb_summary = r.json()
        signal.emit(game)
    except Exception as e:
        print('Error getting the protondb.com status:', e)
        signal.emit(None)


def get_protondb_status(game: SteamApp, signal: Signal) -> None:
    """ Downloads the ProtonDB.com status in a separate threads. When done the Qt Signal "signal" is called """
    t = threading.Thread(target=get_protondb_status_thread, args=(game, signal))
    t.start()


def steam_update_ctool(game: SteamApp, new_ctool=None, steam_config_folder='') -> bool:
    """
    Change compatibility tool for 'game_id' to 'new_ctool' in Steam config vdf
    Return Type: bool
    """
    config_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'config.vdf')
    if not os.path.exists(config_vdf_file):
        return False

    game_id = game.app_id

    try:
        d = vdf_safe_load(config_vdf_file)
        c = get_steam_vdf_compat_tool_mapping(d)

        if str(game_id) in c:
            if new_ctool is None:
                c.pop(str(game_id))
            else:
                c.get(str(game_id))['name'] = str(new_ctool)
        else:
            c[str(game_id)] = {"name": str(new_ctool), "config": "", "priority": "250"}

        vdf.dump(d, open(config_vdf_file, 'w'), pretty=True)
    except Exception as e:
        print('Error, could not update Steam compatibility tool to', new_ctool, 'for game',game_id, ':',
              e, ', vdf:', config_vdf_file)
        return False
    return True


def steam_update_ctools(games: dict[SteamApp, str], steam_config_folder='') -> bool:
    """
    Change compatibility tool for multiple games in Steam config vdf.
    Return Type: bool
    """
    config_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'config.vdf')
    if not os.path.exists(config_vdf_file):
        return False

    try:
        d = vdf_safe_load(config_vdf_file)
        c = get_steam_vdf_compat_tool_mapping(d)

        for game, new_ctool in games.items():
            game_id = game.app_id
            if str(game_id) in c:
                if new_ctool is None:
                    c.pop(str(game_id))
                else:
                    c.get(str(game_id))['name'] = str(new_ctool)
            else:
                c[str(game_id)] = {"name": str(new_ctool), "config": "", "priority": "250"}

        vdf.dump(d, open(config_vdf_file, 'w'), pretty=True)
    except Exception as e:
        print('Error, could not update Steam compatibility tools:', e, ', vdf:', config_vdf_file)
        return False
    return True


def is_steam_running() -> bool:
    """
    Returns True if the Steam client is running, False otherwise
    Return Type: bool
    """
    try:
        procs = os.listdir('/proc')
        for proc in procs:
            exe = os.path.join('/proc', proc, 'exe')
            if os.path.exists(exe) and 'steam' in os.readlink(exe):
                return True
    except:
        pass
    return False


get_fish_user_paths = lambda mfile: ([line.strip() for line in mfile.readlines() if 'fish_user_paths' in line] or ['SETUVAR fish_user_paths:\\x1d'])[0].split('fish_user_paths:')[1:][0].split('\\x1e')


def get_external_steamtinkerlaunch_intall(compat_folder):

    symlink_path = os.path.join(compat_folder, 'steamtinkerlaunch')
    return os.path.dirname(os.readlink(symlink_path)) if os.path.exists(symlink_path) and os.readlink(symlink_path) != os.path.join(STEAM_STL_INSTALL_PATH, 'prefix', 'steamtinkerlaunch') else None


def remove_steamtinkerlaunch(compat_folder='', remove_config=True, ctmod_object=None) -> bool:
    """
    Removes SteamTinkerLaunch from system by removing the downloaad, removing from path
    removing config files at `$HOME/.config/steamtinkerlaunch`.
    
    Returns True if successfully removed.
    Reutrn Type: bool
    """

    try:
        os.chdir(HOME_DIR)

        # If the Steam Deck/ProtonUp-Qt installation path doesn't exist
        # Adding `prefix` to path to be especially sure the user didn't just make an `stl` folder
        #
        # STL script is always named `steamtinkerlaunch`    
        stl_symlink_path = get_external_steamtinkerlaunch_intall(compat_folder)

        if os.path.exists(compat_folder):
            print('Removing SteamTinkerLaunch compatibility tool...')
            shutil.rmtree(compat_folder)
            if shutil.which('steamtinkerlaunch'):
                subprocess.run(['steamtinkerlaunch', 'compat', 'del'])

        print('Removing SteamTinkerLaunch installation...')
        if stl_symlink_path:
            # If STL symlink isn't a regular install, try to remove if we can write to its install folder
            if os.access(stl_symlink_path, os.W_OK):
                shutil.rmtree(stl_symlink_path)
                print('Removed SteamTinkerLaunch installation folder pointed to by symlink')
            else:
                # If we can't remove the actual installation folder, tell the user to remove it themselves and continue with the rest of the uninstallation
                mb_title = QApplication.instance().translate('steamutil.py', 'Unable to Remove SteamTinkerLaunch')
                mb_text = QApplication.instance().translate(
                    'steamutil.py',
                    'Access to SteamTinkerLaunch installation folder at \'{STL_SYMLINK_PATH}\' was denied, please remove this folder manually.\n\nThe uninstallation will continue.'
                ).format(STL_SYMLINK_PATH=stl_symlink_path)
                if ctmod_object and hasattr(ctmod_object, 'message_box_message'):
                    ctmod_object.message_box_message.emit(mb_title, mb_text, QMessageBox.Icon.Warning)
                else:
                    mb = QMessageBox()
                    mb.setWindowTitle(mb_title)
                    mb.setText(mb_text)
                    mb.exec()

                print(f'Error: SteamTinkerLaunch is installed to {stl_symlink_path}, ProtonUp-Qt cannot modify this folder. Folder must be removed manually.')
        elif os.path.exists(STEAM_STL_INSTALL_PATH):
            # Regular Steam Deck/ProtonUp-Qt installation structure
            if IS_FLATPAK:
                if os.path.exists(os.path.join(STEAM_STL_INSTALL_PATH, 'prefix')):
                    shutil.rmtree(os.path.join(STEAM_STL_INSTALL_PATH, 'prefix'))
            else:
                shutil.rmtree(STEAM_STL_INSTALL_PATH)

        # Remove User config folder if the user requested it
        if os.path.exists(STEAM_STL_CONFIG_PATH) and remove_config:
            print('Removing SteamTInkerLaunch configuration folder...')
            shutil.rmtree(STEAM_STL_CONFIG_PATH)

        # Remove the STL path modification that ProtonUp-Qt may have added during installation from Shell paths
        #
        # Works by getting all the lines in all the hardcoded Shell files that we write out to during installation and
        # and filtering out any line(s) that reference ProtonUp-Qt, then it writes that updated file content back out to the Shell file
        present_shell_files = [
            os.path.join(HOME_DIR, f) for f in os.listdir(HOME_DIR) if os.path.isfile(os.path.join(HOME_DIR, f)) and f in STEAM_STL_SHELL_FILES
        ]
        if os.path.exists(STEAM_STL_FISH_VARIABLES) or shutil.which('fish'):
            present_shell_files.append(STEAM_STL_FISH_VARIABLES)

        print('Removing SteamTinkerLaunch from path...')

        for shell_file in present_shell_files:
            with open(shell_file, 'r+') as mfile:  
                # Get all Shell file lines that are not the ProtonUp-Qt added STL path lines              
                mfile_lines = list(filter(lambda l: 'protonup-qt' not in l.lower() and STEAM_STL_INSTALL_PATH.lower() not in l.lower(), list(mfile.readlines())))
                if len(mfile_lines) == 0:
                    continue
                mfile_lines = mfile_lines[:-1] if len(mfile_lines[-1].strip()) == 0 else mfile_lines

                # Preserve any existing Fish user paths
                if 'fish' in mfile.name:
                    mfile.seek(0)
                    curr_fish_user_paths = list(filter(lambda path: STEAM_STL_INSTALL_PATH not in path, list(get_fish_user_paths(mfile))))
                    updated_fish_user_paths = '\\x1e'.join(curr_fish_user_paths)
                    mfile_lines.append(f'SETUVAR fish_user_paths:{updated_fish_user_paths}')

                # Write out changes while preserving Shell file newlines
                mfile.seek(0)
                prev_line = ''
                for line in mfile_lines:
                    if len(line.strip()) != 0 or len(prev_line.strip()) != 0:
                        mfile.write(line)
                    prev_line = line
                mfile.truncate()

        print('Successfully uninstalled SteamTinkerLaunch!')
        return True
    except IOError as e:
        print('Something went wrong trying to uninstall SteamTinkerLaunch. Aborting...', e)
        return False


def install_steam_library_shortcut(steam_config_folder: str, remove_shortcut=False) -> int:
    """
    Adds a shortcut to launch this app to the Steam Library
    Return: 0=success, 1=error, 2=already installed
    """
    users_folder = os.path.realpath(os.path.join(os.path.expanduser(steam_config_folder), os.pardir, 'userdata'))

    try:
        if not os.path.isfile(APP_ICON_FILE):
            with open(APP_ICON_FILE, 'wb') as f:
                f.write(pkgutil.get_data(__name__, 'resources/img/appicon256.png'))

        for userf in os.listdir(users_folder):
            user_cfg_dir = os.path.join(users_folder, userf, 'config')
            shortcuts_file = os.path.join(user_cfg_dir, 'shortcuts.vdf')

            if not os.path.exists(user_cfg_dir):
                continue

            shortcuts_vdf = {}
            sid=-1
            if os.path.exists(shortcuts_file):
                with open(shortcuts_file, 'rb') as f:
                    shortcuts_vdf = vdf.binary_load(f)
                    
                    for sid in list(shortcuts_vdf.get('shortcuts', {}).keys()):
                        svalue = shortcuts_vdf.get('shortcuts', {}).get(sid)
                        if APP_NAME in svalue.get('AppName', ''):
                            if remove_shortcut:
                                shortcuts_vdf.get('shortcuts', {}).pop(sid)
                            else:
                                return 2

            with open(shortcuts_file, 'wb') as f:
                if not remove_shortcut:
                    run_config = ['', '']
                    if IS_FLATPAK:
                        run_config = [f'/usr/bin/flatpak', f'run {APP_ID}']
                    elif exe := subprocess.run(['which', APP_ID], universal_newlines=True, stdout=subprocess.PIPE).stdout.strip():
                        run_config = [exe, '']
                    elif exe := os.getenv('APPIMAGE'):
                        if APP_NAME in exe:
                            exe = os.path.join(exe, os.pardir, APP_NAME + '*.AppImage')  # remove version from file name
                        run_config = [exe, '']
                    else:
                        return 1

                    sid = str(int(sid) + 1)
                    shortcuts_vdf.setdefault('shortcuts', {})[sid] = {
                        'appid': 1621167219,
                        'AppName': APP_NAME,
                        'Exe': f'"{run_config[0]}"',
                        'StartDir': './',
                        'icon': APP_ICON_FILE,
                        'ShortcutPath': '',
                        'LaunchOptions': run_config[1],
                        'IsHidden': 0,
                        'AllowDesktopConfig': 1,
                        'AllowOverlay': 1,
                        'OpenVR': 0,
                        'Devkit': 0,
                        'DevkitGameID': '',
                        'DevkitOverrideAppID': 0,
                        'LastPlayTime': 0,
                        'FlatpakAppID': '',
                        'tags': {}
                    }

                f.write(vdf.binary_dumps(shortcuts_vdf))
    except Exception as e:
        print(f'Error: Could not add {APP_NAME} as Steam shortcut:', e)

    return 0


def write_steam_shortcuts_list(steam_config_folder: str, shortcuts: list[SteamApp], delete_sids: list[int]) -> None:
    """
    Updates the Steam shortcuts.vdf file with the provided shortcuts
    It will update existing shortcuts and add new ones

    Parameters:
        steam_config_folder: str
            Path to the Steam config folder, e.g. '/home/user/.steam/root/config'
        shortcuts: list[SteamApp]
            List of shortcuts to add/update
        delete_sids: list[int]
            List of shortcut ids to delete
    """
    users_folder = os.path.realpath(os.path.join(os.path.expanduser(steam_config_folder), os.pardir, 'userdata'))

    # group shortcuts by user like this: {user1: {sid1: shortcut1, sid2: shortcut2}, user2: {sid3: shortcut3}}
    shortcuts_by_user: dict[dict[SteamApp]] = {}
    for shortcut in shortcuts:
        if shortcut.shortcut_user not in shortcuts_by_user:
            shortcuts_by_user[shortcut.shortcut_user] = {}
        shortcuts_by_user[shortcut.shortcut_user][shortcut.shortcut_id] = shortcut

    for userf in shortcuts_by_user:
        shortcuts_file = os.path.join(users_folder, userf, 'config', 'shortcuts.vdf')

        # read shortcuts.vdf
        shortcuts_vdf = {}
        with open(shortcuts_file, 'rb') as f:
            shortcuts_vdf = vdf.binary_load(f)
        current_shortcuts = shortcuts_vdf.get('shortcuts', {})

        # update/add new shortcuts
        modified_shorcuts = shortcuts_by_user.get(userf, {})
        for sid in list(modified_shorcuts.keys()):
            shortcut_modified: SteamApp = modified_shorcuts.get(sid)
            if sid in current_shortcuts:  # update existing shortcut
                svalue_current = current_shortcuts.get(sid)
                svalue_current['AppName'] = shortcut_modified.game_name
                svalue_current['Exe'] = shortcut_modified.shortcut_exe
                svalue_current['StartDir'] = shortcut_modified.shortcut_startdir
                svalue_current['icon'] = shortcut_modified.shortcut_icon
            else:  # add a new shortcut to shortcuts.vdf
                svalue_new = {
                    'appid': shortcut_modified.app_id,
                    'AppName': shortcut_modified.game_name,
                    'Exe': shortcut_modified.shortcut_exe,
                    'StartDir': shortcut_modified.shortcut_startdir,
                    'icon': shortcut_modified.shortcut_icon,
                    'ShortcutPath': '',
                    'LaunchOptions': '',
                    'IsHidden': 0,
                    'AllowDesktopConfig': 1,
                    'AllowOverlay': 1,
                    'OpenVR': 0,
                    'Devkit': 0,
                    'DevkitGameID': '',
                    'DevkitOverrideAppID': 0,
                    'LastPlayTime': 0,
                    'FlatpakAppID': '',
                    'tags': {}
                }
                current_shortcuts[sid] = svalue_new

        # delete shortcuts that are marked for deletion
        for sid in delete_sids:
            current_shortcuts.pop(sid)

        # write shortcuts.vdf
        try:
            with open(shortcuts_file, 'wb') as f:
                f.write(vdf.binary_dumps(shortcuts_vdf))
        except Exception as e:
            print(f'Error: Could not write_steam_shortcuts_list for user {userf}:', e)


def calc_shortcut_app_id(appname: str, exe: str) -> int:
    """
    Calculates an app id for a shortcut based on the app name and executable.
    Based on https://github.com/SteamGridDB/steam-rom-manager/blob/master/src/lib/helpers/steam/generate-app-id.ts

    Parameters:
        appname: str
            game_name of the shortcut
        exe: str
            shortcut_exe

    Returns:
        int
    """
    key = exe + appname
    return (binascii.crc32(key.encode()) | 0x80000000) - 0x100000000


def get_steam_user_list(steam_config_folder: str) -> list[SteamUser]:
    """
    Returns a list of Steam users

    Parameters:
        steam_config_folder: str
            e.g. '~/.steam/root/config'

    Return Type: list[SteamUser]
    """
    loginusers_vdf_file = os.path.join(os.path.expanduser(steam_config_folder), 'loginusers.vdf')

    users = []

    if not os.path.exists(loginusers_vdf_file):
        print(f'Warning: Loginusers file does not exist at {loginusers_vdf_file}')
        return []

    try:
        d = vdf_safe_load(loginusers_vdf_file)
        u = d.get('users', {})
        for uid in list(u.keys()):
            uvalue = u.get(uid, {})

            user = SteamUser()
            user.long_id = int(uid)
            user.account_name = uvalue.get('AccountName', '')
            user.persona_name = uvalue.get('PersonaName', '')
            user.most_recent = bool(int(uvalue.get('MostRecent', '0')))
            user.timestamp = int(uvalue.get('Timestamp', '-1'))

            users.append(user)
    except Exception as e:
        print('Error (get_steam_user_list): Could not get a list of Steam users:', e)

    return users


def determine_most_recent_steam_user(steam_users: list[SteamUser]) -> SteamUser:
    """
    Returns the Steam user that was logged-in most recent, otherwise the first user or None.
    Looks for the first user with most_recent=True.

    Parameters:
        steam_users: list[SteamUser]
            list of steam users, from get_steam_user_list()

    Return Type: SteamUser|None
    """
    for user in steam_users:
        if user.most_recent == True:
            return user

    if len(steam_users) > 0:
        print(f'Warning: There is no most recent Steam user. Returning the first user {steam_users[0].get_short_id()}')
        return steam_users[0]

    print('Warning: No Steam users found. Returning None')
    return None


def is_valid_steam_install(steam_path) -> bool:

    """
    Return whether required Steam data files actually exist to determine if 'steam_path' is a valid Steam installation.
    Return Type: bool
    """

    ct_dir = os.path.join(os.path.expanduser(steam_path), 'config')

    config_vdf = os.path.join(ct_dir, 'config.vdf')
    libraryfolders_vdf = os.path.join(ct_dir, 'libraryfolders.vdf')

    is_valid_steam_install = os.path.exists(config_vdf) and os.path.exists(libraryfolders_vdf)

    return is_valid_steam_install

def vdf_safe_load(vdf_file: str) -> dict:
    """
    Loads a vdf file and returns its contents as a dict.
    In case of an error, the error is printed and {} is returned.

    Args:
        vdf_file (str): Path to the vdf file

    Returns:
        dict (empty in case of an error)
    """
    data = {}

    try:
        # See https://github.com/DavidoTek/ProtonUp-Qt/issues/424 (unicode errors)
        with open(vdf_file, 'r', encoding='utf-8', errors='replace') as f:
            data = vdf.loads(f.read())
    except Exception as e:
        print(f'An error occured while calling vdf_safe_load("{vdf_file}"). Returning empty dict: {e}')

    if not isinstance(data, dict):
        # Apparently, vdf.loads() can return None (issue #481)
        print(f'Warning (vdf_safe_load): vdf.loads("{vdf_file}") returned {data}. Returning empty dict.')
        data = {}

    return data
