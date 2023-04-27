import inspect

import datajoint as dj
import pandas as pd

import aeon
from aeon.dj_pipeline import acquisition, get_schema_name
from aeon.io import api as io_api

logger = dj.logger


# schema_name = f'u_{dj.config["database.user"]}_streams'  # for testing
schema_name = get_schema_name("streams")
schema = dj.schema(schema_name)

schema.spawn_missing_classes()


@schema
class StreamType(dj.Lookup):
    """
    Catalog of all steam types for the different device types used across Project Aeon
    One StreamType corresponds to one reader class in `aeon.io.reader`
    The combination of `stream_reader` and `stream_reader_kwargs` should fully specify
    the data loading routine for a particular device, using the `aeon.io.utils`
    """

    definition = """  # Catalog of all stream types used across Project Aeon
    stream_type          : varchar(20)
    ---
    stream_reader        : varchar(256)     # name of the reader class found in `aeon_mecha` package (e.g. aeon.io.reader.Video)
    stream_reader_kwargs : longblob  # keyword arguments to instantiate the reader class
    stream_description='': varchar(256)
    stream_hash          : uuid    # hash of dict(stream_reader_kwargs, stream_reader=stream_reader)
    unique index (stream_hash)
    """


@schema
class DeviceType(dj.Lookup):
    """
    Catalog of all device types used across Project Aeon
    """

    definition = """  # Catalog of all device types used across Project Aeon
    device_type:             varchar(36)
    ---
    device_description='':   varchar(256)
    """

    class Stream(dj.Part):
        definition = """  # Data stream(s) associated with a particular device type
        -> master
        -> StreamType
        """


@schema
class Device(dj.Lookup):
    definition = """  # Physical devices, of a particular type, identified by unique serial number
    device_serial_number: varchar(12)
    ---
    -> DeviceType
    """


# region Helper functions for creating device tables.


def get_device_template(device_type: str):
    """Returns table class template for ExperimentDevice"""
    device_title = device_type
    device_type = dj.utils.from_camel_case(device_type)

    class ExperimentDevice(dj.Manual):
        definition = f"""
        # {device_title} placement and operation for a particular time period, at a certain location, for a given experiment (auto-generated with aeon_mecha-{aeon.__version__})
        -> acquisition.Experiment
        -> Device
        {device_type}_install_time  : datetime(6)   # time of the {device_type} placed and started operation at this position
        ---
        {device_type}_name          : varchar(36)
        """

        class Attribute(dj.Part):
            definition = """  # metadata/attributes (e.g. FPS, config, calibration, etc.) associated with this experimental device
            -> master
            attribute_name          : varchar(32)
            ---
            attribute_value=null    : longblob
            """

        class RemovalTime(dj.Part):
            definition = f"""
            -> master
            ---
            {device_type}_removal_time: datetime(6)  # time of the {device_type} being removed
            """

    ExperimentDevice.__name__ = f"{device_title}"

    return ExperimentDevice


def get_device_stream_template(device_type: str, stream_type: str):
    """Returns table class template for DeviceDataStream"""
    
    context = inspect.currentframe().f_back.f_locals["context"]
    ExperimentDevice = context[device_type]
    
    # DeviceDataStream table(s)
    stream_detail = (
        StreamType
        & (DeviceType.Stream & {"device_type": device_type, "stream_type": stream_type})
    ).fetch1()

    for i, n in enumerate(stream_detail["stream_reader"].split(".")):
        if i == 0:
            reader = aeon
        else:
            reader = getattr(reader, n)

    stream = reader(**stream_detail["stream_reader_kwargs"])

    table_definition = f"""  # Raw per-chunk {stream_type} data stream from {device_type} (auto-generated with aeon_mecha-{aeon.__version__})
        -> {device_type}
        -> acquisition.Chunk
        ---
        sample_count: int      # number of data points acquired from this stream for a given chunk
        timestamps: longblob   # (datetime) timestamps of {stream_type} data
        """

    for col in stream.columns:
        if col.startswith("_"):
            continue
        table_definition += f"{col}: longblob\n\t\t\t"

    class DeviceDataStream(dj.Imported):
        definition = table_definition
        _stream_reader = reader
        _stream_detail = stream_detail

        @property
        def key_source(self):
            f"""
            Only the combination of Chunk and {device_type} with overlapping time
            +  Chunk(s) that started after {device_type} install time and ended before {device_type} remove time
            +  Chunk(s) that started after {device_type} install time for {device_type} that are not yet removed
            """
            return (
                acquisition.Chunk
                * ExperimentDevice.join(ExperimentDevice.RemovalTime, left=True)
                & f"chunk_start >= {dj.utils.from_camel_case(device_type)}_install_time"
                & f'chunk_start < IFNULL({dj.utils.from_camel_case(device_type)}_removal_time, "2200-01-01")'
            )

        def make(self, key):
            chunk_start, chunk_end, dir_type = (acquisition.Chunk & key).fetch1(
                "chunk_start", "chunk_end", "directory_type"
            )
            raw_data_dir = acquisition.Experiment.get_data_directory(
                key, directory_type=dir_type
            )

            device_name = (ExperimentDevice & key).fetch1(f"{dj.utils.from_camel_case(device_type)}_name")

            stream = self._stream_reader(
                **{
                    k: v.format(**{k: device_name}) if k == "pattern" else v
                    for k, v in self._stream_detail["stream_reader_kwargs"].items()
                }
            )

            stream_data = io_api.load(
                root=raw_data_dir.as_posix(),
                reader=stream,
                start=pd.Timestamp(chunk_start),
                end=pd.Timestamp(chunk_end),
            )

            self.insert1(
                {
                    **key,
                    "sample_count": len(stream_data),
                    "timestamps": stream_data.index.values,
                    **{
                        c: stream_data[c].values
                        for c in stream.columns
                        if not c.startswith("_")
                    },
                }
            )

    DeviceDataStream.__name__ = f"{device_type}{stream_type}"

    return DeviceDataStream


# endregion



def main(context=None):
    if context is None:
        context = inspect.currentframe().f_back.f_locals

    # Create tables.
    for device_info in (DeviceType).fetch(as_dict=True):
        table_class = get_device_template(device_info["device_type"])
        context[table_class.__name__] = table_class
        schema(table_class, context=context)
        
    # Create DeviceDataStream tables.
    for device_info in (DeviceType.Stream).fetch(as_dict=True):
        table_class = get_device_stream_template(
            device_info["device_type"], device_info["stream_type"]
        )
        context[table_class.__name__] = table_class
        schema(table_class, context=context)

main()