#! /usr/bin/env python
#
# Copyright 2021 Spotify AB
#
# Licensed under the GNU Public License, Version 3.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.gnu.org/licenses/gpl-3.0.html
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import time
import math
import psutil
import atexit
import random
import shutil
import platform
import subprocess
from glob import glob
from pathlib import Path

from pedalboard._pedalboard import (
    WrappedBool,
    strip_common_float_suffixes,
    normalize_python_parameter_name,
)
import pytest
import pedalboard
import numpy as np
from typing import Optional


TEST_PLUGIN_BASE_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "plugins")
TEST_PRESET_BASE_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "presets")

AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT = [
    os.path.basename(filename)
    for filename in glob(os.path.join(TEST_PLUGIN_BASE_PATH, platform.system(), "*"))
]

# Disable Audio Unit tests on GitHub Actions, as the
# action container fails to load Audio Units:
if os.getenv("CIBW_TEST_REQUIRES") or os.getenv("CI"):
    AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT = [
        f for f in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT if "component" not in f
    ]


def is_container_plugin(filename: str):
    for klass in pedalboard._AVAILABLE_PLUGIN_CLASSES:
        try:
            return len(klass.get_plugin_names_for_file(filename)) > 1
        except ImportError:
            pass
    return False


AVAILABLE_CONTAINER_PLUGINS_IN_TEST_ENVIRONMENT = [
    filename for filename in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT if is_container_plugin(filename)
]


def get_parameters(plugin_filename: str):
    return load_test_plugin(plugin_filename).parameters


TEST_PLUGIN_CACHE = {}
TEST_PLUGIN_ORIGINAL_PARAMETER_CACHE = {}


PLUGIN_FILES_TO_DELETE = set()
MACOS_PLUGIN_INSTALL_PATH = Path.home() / "Library" / "Audio" / "Plug-Ins" / "Components"


def load_test_plugin(plugin_filename: str, disable_caching: bool = False, *args, **kwargs):
    """
    Load a plugin file from disk, or use an existing instance to save
    on test runtime if we already have one in memory.

    The plugin filename is used here rather than the fully-qualified path
    to reduce line length in the pytest output.
    """

    key = repr((plugin_filename, args, tuple(kwargs.items())))
    if key not in TEST_PLUGIN_CACHE or disable_caching:
        plugin_path = os.path.join(TEST_PLUGIN_BASE_PATH, platform.system(), plugin_filename)

        if platform.system() == "Darwin" and plugin_filename.endswith(".component"):
            # On macOS, AudioUnit components must be installed in
            # ~/Library/Audio/Plug-Ins/Components in order to be loaded.
            installed_plugin_path = os.path.join(MACOS_PLUGIN_INSTALL_PATH, plugin_filename)
            plugin_already_installed = os.path.exists(installed_plugin_path)
            if not plugin_already_installed:
                shutil.copytree(plugin_path, installed_plugin_path)
                PLUGIN_FILES_TO_DELETE.add(installed_plugin_path)
            plugin_path = installed_plugin_path

        # Try to load a given plugin multiple times if necessary.
        # Unsure why this is necessary, but it seems this only happens in test.
        exception = None
        for attempt in range(5):
            try:
                plugin = pedalboard.load_plugin(plugin_path, *args, **kwargs)
                break
            except ImportError as e:
                exception = e
                time.sleep(attempt)
        else:
            raise exception

        if disable_caching:
            return plugin
        TEST_PLUGIN_CACHE[key] = plugin
        TEST_PLUGIN_ORIGINAL_PARAMETER_CACHE[key] = {
            key: getattr(plugin, key) for key in plugin.parameters.keys()
        }

    # Try to reset default parameters when loading:
    plugin = TEST_PLUGIN_CACHE[key]
    for name in plugin.parameters.keys():
        try:
            setattr(plugin, name, TEST_PLUGIN_ORIGINAL_PARAMETER_CACHE[key][name])
        except ValueError:
            pass

    # Force a reset:
    plugin.reset()
    return plugin


def delete_installed_plugins():
    for plugin_file in PLUGIN_FILES_TO_DELETE:
        shutil.rmtree(plugin_file)


atexit.register(delete_installed_plugins)


# Allow testing with all of the plugins on the local machine:
if os.environ.get("ENABLE_TESTING_WITH_LOCAL_PLUGINS", False):
    for plugin_class in pedalboard._AVAILABLE_PLUGIN_CLASSES:
        for plugin_path in plugin_class.installed_plugins:
            try:
                load_test_plugin(plugin_path)
                AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT.append(plugin_path)
            except Exception as e:
                print(f"Tried to load {plugin_path} for local testing, but failed with: {e}")

            # Even if the plugin failed to load, add it to
            # the list of known container plugins if necessary:
            if is_container_plugin(plugin_path):
                AVAILABLE_CONTAINER_PLUGINS_IN_TEST_ENVIRONMENT.append(plugin_path)


def plugin_named(*substrings: str) -> Optional[str]:
    """
    Return the first plugin filename that contains all of the
    provided substrings from the list of available test plugins.
    """
    for plugin_filename in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT:
        if all([s in plugin_filename for s in substrings]):
            return plugin_filename


def max_volume_of(x: np.ndarray) -> float:
    return np.amax(np.abs(x))


def test_at_least_one_plugin_is_available_for_testing():
    assert AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT


@pytest.mark.parametrize(
    "value,expected",
    [
        ("nope", "nope"),
        ("10.5x", "10.5"),
        ("12%", "12"),
        ("123 Hz", "123"),
        ("123.45 Hz", "123.45"),
        ("123.45 kHz", "123450.0"),
        ("kHz", "kHz"),
        (1.23, 1.23),
        ("one thousand kHz", "one thousand kHz"),
    ],
)
def test_strip_common_float_suffixes(value, expected):
    actual = strip_common_float_suffixes(value)
    assert actual == expected


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_at_least_one_parameter(plugin_filename: str):
    """
    Many tests below are parametrized on the parameters of the plugin;
    if our parameter parsing code fails, those tests won't fail as
    there will just be no code to run. This works around that problem.
    """

    assert get_parameters(plugin_filename)


@pytest.mark.parametrize(
    "plugin_filename,plugin_preset",
    [
        (plugin, os.path.join(TEST_PRESET_BASE_PATH, plugin + ".vstpreset"))
        for plugin in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        if os.path.isfile(os.path.join(TEST_PRESET_BASE_PATH, plugin + ".vstpreset"))
    ],
)
def test_preset_parameters(plugin_filename: str, plugin_preset: str):
    # plugin with default params.
    plugin = load_test_plugin(plugin_filename)

    default_params = {k: v.raw_value for k, v in plugin.parameters.items() if v.type == float}

    # load preset file
    plugin.load_preset(plugin_preset)

    for name, default in default_params.items():
        actual = getattr(plugin, name)
        if math.isnan(actual):
            continue
        assert (
            actual != default
        ), f"Expected attribute {name} to be different from default ({default}), but was {actual}"


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_initial_parameters(plugin_filename: str):
    parameters = {
        k: v.min_value for k, v in get_parameters(plugin_filename).items() if v.type == float
    }

    # Reload the plugin, but set the initial parameters in the load call.
    plugin = load_test_plugin(plugin_filename, parameter_values=parameters)

    for name, expected in parameters.items():
        actual = getattr(plugin, name)
        if math.isnan(actual):
            continue
        assert actual == expected, f"Expected attribute {name} to be {expected}, but was {actual}"


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == float]
    ],
)
def test_initial_parameter_validation(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)
    with pytest.raises(ValueError):
        load_test_plugin(
            plugin_filename,
            parameter_values={parameter_name: getattr(plugin, parameter_name).max_value + 100},
        )


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_initial_parameter_validation_missing(plugin_filename: str):
    with pytest.raises(AttributeError):
        load_test_plugin(plugin_filename, parameter_values={"missing_parameter": 123})


@pytest.mark.parametrize("loader", [pedalboard.load_plugin] + pedalboard._AVAILABLE_PLUGIN_CLASSES)
def test_import_error_on_missing_path(loader):
    with pytest.raises(ImportError):
        loader("./")


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
@pytest.mark.parametrize(
    "num_channels,sample_rate",
    [(1, 48000), (2, 48000), (1, 44100), (2, 44100), (1, 22050), (2, 22050)],
)
def test_plugin_accepts_variable_channel_count(
    plugin_filename: str, num_channels: int, sample_rate: float
):
    plugin = load_test_plugin(plugin_filename)
    noise = np.random.rand(num_channels, sample_rate)
    try:
        effected = plugin(noise, sample_rate)
        assert effected.shape == noise.shape
    except ValueError as e:
        if "does not support" not in str(e):
            raise


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_plugin_accepts_variable_channel_count_without_reloading(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename)
    for num_channels, sample_rate in [
        (1, 48000),
        (2, 48000),
        (1, 44100),
        (2, 44100),
        (1, 22050),
        (2, 22050),
    ]:
        noise = np.random.rand(num_channels, sample_rate)
        try:
            effected = plugin(noise, sample_rate)
            assert effected.shape == noise.shape
        except ValueError as e:
            if "does not support" not in str(e):
                raise


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_all_parameters_are_accessible_as_properties(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename)
    assert plugin.parameters
    for parameter_name in plugin.parameters.keys():
        assert hasattr(plugin, parameter_name)


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_parameters_cant_be_assigned_to_directly(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename)
    assert plugin.parameters
    for parameter_name in plugin.parameters.keys():
        current_value = getattr(plugin, parameter_name)
        with pytest.raises(TypeError):
            plugin.parameters[parameter_name] = current_value


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_all_parameters_have_accessors(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename)
    assert plugin.parameters
    for parameter_name, parameter in plugin.parameters.items():
        assert parameter_name in dir(plugin)
        parameter_value = getattr(plugin, parameter_name)
        assert hasattr(parameter_value, "raw_value")
        assert repr(parameter)
        assert isinstance(parameter_value, (float, WrappedBool, str))
        assert parameter_value.raw_value == parameter.raw_value
        assert parameter_value.range is not None


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_attributes_proxy(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename)

    # Should be disallowed:
    with pytest.raises(AttributeError):
        plugin.parameters = "123"

    # Should be allowed:
    plugin.new_parameter = "123"


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == bool]
    ],
)
def test_bool_parameters(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)

    parameter_value = getattr(plugin, parameter_name)
    assert repr(parameter_value) in ("True", "False")
    assert parameter_value in (True, False)
    # Flip the parameter and ensure that it does change:
    setattr(plugin, parameter_name, not parameter_value)
    assert getattr(plugin, parameter_name) != parameter_value

    # Ensure that if we access an attribute that we're not adding to the value,
    # we fall back to the underlying type (bool) or we raise an exception if not:
    assert hasattr(parameter_value, "bit_length")
    with pytest.raises(AttributeError):
        parameter_value.something_that_doesnt_exist


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == bool]
    ],
)
def test_bool_parameter_valdation(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)
    with pytest.raises(ValueError):
        setattr(plugin, parameter_name, 123.4)


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == float]
    ],
)
def test_float_parameters(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)
    parameter_value = getattr(plugin, parameter_name)
    assert repr(parameter_value) == repr(float(parameter_value))
    assert isinstance(parameter_value, float)
    # Change the parameter and ensure that it does change:
    new_values = parameter_value.valid_values

    if parameter_value.step_size is not None:
        _min, _max, _ = parameter_value.range
        step_size = parameter_value.step_size
        new_values = np.arange(_min, _max, step_size)
        # Because sometimes np.arange(_min, _max) gives values _slightly outside_
        # of [_min, _max] thanks to floating point:
        new_values = [max(_min, min(_max, new_value)) for new_value in new_values]

    epsilon = parameter_value.step_size or parameter_value.approximate_step_size or 0.001

    for new_value in new_values:
        setattr(plugin, parameter_name, new_value)
        if math.isnan(getattr(plugin, parameter_name)):
            continue
        assert math.isclose(new_value, getattr(plugin, parameter_name), abs_tol=epsilon * 2.0)

    # Ensure that if we access an attribute that we're not adding to the value,
    # we fall back to the underlying type (float) or we raise an exception if not:
    assert parameter_value.real == float(parameter_value)
    with pytest.raises(AttributeError):
        parameter_value.something_that_doesnt_exist


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == float]
    ],
)
def test_float_parameter_valdation(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)

    parameter = getattr(plugin, parameter_name)

    with pytest.raises(ValueError):
        setattr(plugin, parameter_name, "not a float")

    # Should be allowed to set a float parameter followed by its label:
    if parameter.label:
        setattr(plugin, parameter_name, f"{parameter} {parameter.label}")

    # ...but not by a different label:
    if parameter.label != "dB":
        with pytest.raises(ValueError):
            setattr(plugin, parameter_name, f"{parameter} dB")

    max_range = parameter.max_value
    with pytest.raises(ValueError):
        setattr(plugin, parameter_name, max_range + 100)

    min_range = parameter.min_value
    with pytest.raises(ValueError):
        setattr(plugin, parameter_name, min_range - 100)


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == str]
    ],
)
def test_str_parameters(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)
    parameter_value = getattr(plugin, parameter_name)
    assert repr(parameter_value) == repr(parameter_value)
    assert isinstance(parameter_value, str)
    # Change the parameter and ensure that it does change:
    for new_value in parameter_value.valid_values:
        setattr(plugin, parameter_name, new_value)
        assert getattr(plugin, parameter_name) == new_value

    # Ensure that if we access an attribute that we're not adding to the value,
    # we fall back to the underlying type (str) or we raise an exception if not:
    assert parameter_value.lower()
    with pytest.raises(AttributeError):
        parameter_value.something_that_doesnt_exist


@pytest.mark.parametrize(
    "plugin_filename,parameter_name",
    [
        (path, parameter)
        for path in AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT
        for parameter in [k for k, v in get_parameters(path).items() if v.type == str]
    ],
)
def test_string_parameter_valdation(plugin_filename: str, parameter_name: str):
    plugin = load_test_plugin(plugin_filename)
    some_other_object = object()

    with pytest.raises(ValueError):
        setattr(plugin, parameter_name, some_other_object)

    with pytest.raises(ValueError):
        setattr(plugin, parameter_name, "some value not present")


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_plugin_parameters_persist_between_calls(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename)
    sr = 44100
    noise = np.random.rand(2, sr)

    # Set random parameter values on the plugin:
    for name, parameter in plugin.parameters.items():
        if name == "program":
            continue
        if parameter.type == float:
            low, high, step = parameter.range
            if not step:
                step = 0.1
            if low is None:
                low = 0.0
            if high is None:
                high = 1.0
            values = [
                x * step for x in list(range(int(low / step), int(high / step), 1)) + [high / step]
            ]
            random_value = random.choice(values)
        elif parameter.type == bool:
            random_value = bool(random.random())
        elif parameter.type == str:
            if parameter.valid_values:
                random_value = random.choice(parameter.valid_values)
            else:
                random_value = None
        if (
            random_value is not None
            and "bypass" not in name.lower()
            and "preset" not in name.lower()
        ):
            setattr(plugin, name, random_value)

    expected_values = {}
    for name, parameter in plugin.parameters.items():
        expected_values[name] = getattr(plugin, name)

    plugin.process(noise, sr)

    for name, parameter in plugin.parameters.items():
        assert (
            getattr(plugin, name) == expected_values[name]
        ), f"Expected {name} to match saved value"


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_plugin_state_cleared_between_invocations_by_default(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename, disable_caching=True)

    sr = 44100
    noise = np.random.rand(sr, 2)
    assert max_volume_of(noise) > 0.95
    silence = np.zeros_like(noise)
    assert max_volume_of(silence) == 0.0

    assert max_volume_of(plugin(silence, sr)) < 0.00001
    assert max_volume_of(plugin(noise, sr)) > 0.00001
    for _ in range(10):
        assert max_volume_of(silence) == 0.0
        assert max_volume_of(plugin(silence, sr)) < 0.00001


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_plugin_state_not_cleared_between_invocations_if_reset_is_false(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename, disable_caching=True)

    sr = 44100
    noise = np.random.rand(sr, 2)
    assert max_volume_of(noise) > 0.95
    silence = np.zeros_like(noise)
    assert max_volume_of(silence) == 0.0

    assert max_volume_of(plugin(silence, sr, reset=False)) < 0.00001
    assert max_volume_of(plugin(noise, sr, reset=False)) > 0.00001
    assert max_volume_of(plugin(silence, sr, reset=False)) > 0.00001


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_explicit_reset(plugin_filename: str):
    plugin = load_test_plugin(plugin_filename, disable_caching=True)

    sr = 44100
    noise = np.random.rand(sr, 2)
    assert max_volume_of(noise) > 0.95
    silence = np.zeros_like(noise)
    assert max_volume_of(silence) == 0.0

    assert max_volume_of(plugin(silence, sr, reset=False)) < 0.00001
    assert max_volume_of(plugin(noise, sr, reset=False)) > 0.00001
    plugin.reset()
    assert max_volume_of(plugin(silence, sr, reset=False)) < 0.00001


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_explicit_reset_in_pedalboard(plugin_filename: str):
    sr = 44100
    board = pedalboard.Pedalboard([load_test_plugin(plugin_filename, disable_caching=True)])
    noise = np.random.rand(sr, 2)

    assert max_volume_of(noise) > 0.95
    silence = np.zeros_like(noise)
    assert max_volume_of(silence) == 0.0

    assert max_volume_of(board(silence, reset=False, sample_rate=sr)) < 0.00001
    assert max_volume_of(board(noise, reset=False, sample_rate=sr)) > 0.00001
    board.reset()
    assert max_volume_of(board(silence, reset=False, sample_rate=sr)) < 0.00001


@pytest.mark.parametrize("value", (True, False))
def test_wrapped_bool(value: bool):
    wrapped = WrappedBool(value)
    assert wrapped == value
    assert repr(wrapped) == repr(value)
    assert hash(wrapped) == hash(value)
    assert bool(wrapped) == bool(value)
    assert str(wrapped) == str(value)
    for attr in dir(value):
        assert hasattr(wrapped, attr)


def test_wrapped_bool_requires_bool():
    with pytest.raises(TypeError):
        assert WrappedBool(None)


@pytest.mark.parametrize(
    "_input,expected",
    [
        ("C#", "c_sharp"),
        ("B♭", "b_flat"),
        ("Azimuth (º)", "azimuth"),
        ("Normal String", "normal_string"),
    ],
)
def test_parameter_name_normalization(_input: str, expected: str):
    assert normalize_python_parameter_name(_input) == expected


@pytest.mark.skipif(not plugin_named("CHOWTapeModel"), reason="Missing CHOWTapeModel plugin.")
@pytest.mark.parametrize("buffer_size", [16, 128, 8192, 65536])
@pytest.mark.parametrize("oversampling", [1, 2, 4, 8, 16])
def test_external_plugin_latency_compensation(buffer_size: int, oversampling: int):
    """
    This test loads CHOWTapeModel (which has non-zero latency due
    to an internal oversampler), puts it into Bypass mode, then
    ensures that the input matches the output exactly.
    """
    num_seconds = 10.0
    sample_rate = 48000
    noise = np.random.rand(int(num_seconds * sample_rate))

    plugin = load_test_plugin(plugin_named("CHOWTapeModel"), disable_caching=True)
    plugin.bypass = True
    plugin.oversampling = oversampling

    output = plugin.process(noise, sample_rate, buffer_size=buffer_size)
    np.testing.assert_allclose(output, noise, atol=0.05)


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_show_editor(plugin_filename: str):
    # Run this test in a subprocess, as otherwise we'd block this thread:
    full_plugin_filename = os.path.join(TEST_PLUGIN_BASE_PATH, platform.system(), plugin_filename)
    try:
        subprocess.check_output(
            [
                psutil.Process(os.getpid()).exe(),
                "-c",
                "import pedalboard;"
                f'pedalboard.load_plugin(r"{full_plugin_filename}").show_editor();',
            ],
            timeout=5,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        if (
            b"no visual display devices available" in e.output
            # Unsure why, but in some test environments, we
            # can't load Pedalboard in a subprocess.
            # TODO(psobot): Ensure we can load Pedalboard properly
            # in all environments for this test.
            or b"No module named 'pedalboard'" in e.output
        ):
            pass
        else:
            raise
    except subprocess.TimeoutExpired:
        # This is good: the UI was shown, no issues.
        pass


@pytest.mark.skipif(
    not AVAILABLE_CONTAINER_PLUGINS_IN_TEST_ENVIRONMENT,
    reason="No plugin containers installed in test environment!",
)
@pytest.mark.parametrize("plugin_filename", AVAILABLE_CONTAINER_PLUGINS_IN_TEST_ENVIRONMENT)
def test_plugin_container_handling(plugin_filename: str):
    """
    Some plugins can have multiple sub-plugins within them.
    As of v0.4.5, Pedalboard requires indicating the specific plugin to open within the container.
    These plugins will fail by default.
    """
    with pytest.raises(ValueError) as e:
        load_test_plugin(plugin_filename, disable_caching=True)
    assert plugin_filename in str(e)
    assert "plugin_name" in str(e)

    # Hackily parse out the plugin names:
    message = e.value.args[0]
    plugin_names = [line.strip().strip('"') for line in message.split("\n")[1:]]
    assert f"{len(plugin_names)} plugins" in message

    # Try to re-load each of the component plugins here:
    for plugin_name in plugin_names:
        plugin = load_test_plugin(plugin_filename, disable_caching=True, plugin_name=plugin_name)
        assert plugin_name == plugin.name


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_get_plugin_name_from_regular_plugin(plugin_filename: str):
    plugin_path = os.path.join(TEST_PLUGIN_BASE_PATH, platform.system(), plugin_filename)
    if ".vst3" in plugin_filename:
        names = pedalboard.VST3Plugin.get_plugin_names_for_file(plugin_path)
    elif ".component" in plugin_filename:
        names = pedalboard.AudioUnitPlugin.get_plugin_names_for_file(plugin_path)
    else:
        raise ValueError("Plugin does not seem to be a .vst3 or .component.")

    assert len(names) == 1
    assert load_test_plugin(plugin_filename).name == names[0]


@pytest.mark.skipif(
    not AVAILABLE_CONTAINER_PLUGINS_IN_TEST_ENVIRONMENT,
    reason="No plugin containers installed in test environment!",
)
@pytest.mark.parametrize("plugin_filename", AVAILABLE_CONTAINER_PLUGINS_IN_TEST_ENVIRONMENT)
def test_get_plugin_names_from_container(plugin_filename: str):
    plugin_path = os.path.join(TEST_PLUGIN_BASE_PATH, platform.system(), plugin_filename)
    if ".vst3" in plugin_filename:
        names = pedalboard.VST3Plugin.get_plugin_names_for_file(plugin_path)
    elif ".component" in plugin_filename:
        names = pedalboard.AudioUnitPlugin.get_plugin_names_for_file(plugin_path)
    else:
        raise ValueError("Plugin does not seem to be a .vst3 or .component.")

    assert len(names) > 1


@pytest.mark.parametrize("plugin_filename", AVAILABLE_PLUGINS_IN_TEST_ENVIRONMENT)
def test_midi(plugin_filename: str):
    if not '.vst' in plugin_filename:
        return #TODO make midi work with .component plugins
    plugin = load_test_plugin(plugin_filename)
    assert plugin.get_num_midi_events() == 0
    assert plugin.midi_note_on()
    assert plugin.get_num_midi_events() == 1
    assert plugin.midi_note_off(sampleNumber=16000)
    assert plugin.get_num_midi_events() == 2
    plugin.clear_midi()
    assert plugin.get_num_midi_events() == 0
