from enum import IntEnum
from time import gmtime, strftime
import warnings

import pandas as pd
import numpy as np
import pvl
import struct

from plio.io import ControlNetFileV0002_pb2 as cnf
from plio.io import ControlNetFileHeaderV0005_pb2 as cnh5
from plio.io import ControlPointFileEntryV0005_pb2 as cnp5
from plio.utils.utils import xstr, find_in_dict

HEADERSTARTBYTE = 65536
DEFAULTUSERNAME = 'None'


def write_filelist(lst, path="fromlist.lis"):
    """
    Writes a filelist to a file so it can be used in ISIS3.
    Parameters
    ----------
    lst : list
          A list containing full paths to the images used, as strings.
    path : str
           The name of the file to write out. Default: fromlist.lis
    """
    handle = open(path, 'w')
    for filename in lst:
        handle.write(filename)
        handle.write('\n')
    return


class MeasureMessageType(IntEnum):
    """
    An enum to mirror the ISIS3 MeasureLogData enum.
    """
    GoodnessOfFit = 2
    MinimumPixelZScore = 3
    MaximumPixelZScore = 4
    PixelShift = 5
    WholePixelCorrelation = 6
    SubPixelCorrelation = 7

class MeasureLog():

    def __init__(self, messagetype, value):
        """
        A protobuf compliant measure log object.

        Parameters
        ----------
        messagetype : int or str
                      Either the integer or string representation from the MeasureMessageType enum

        value : int or float
                The value to be stored in the message log
        """
        if isinstance(messagetype, int):
            # by value
            self.messagetype = MeasureMessageType(messagetype)
        else:
            # by name
            self.messagetype = MeasureMessageType[messagetype]

        if not isinstance(value, (float, int)):
            raise TypeError(f'{value} is not a numeric type')
        self.value = value

    def __repr__(self):
        return f'{self.messagetype.name}: {self.value}'

    def to_protobuf(self, version=2):
        """
        Return protobuf compliant measure log object representation
        of this class.

        Returns
        -------
        log_message : obj
                      MeasureLogData object suitable to append to a MeasureLog
                      repeated field.
        """
        # I do not see a better way to get to the inner MeasureLogData obj than this
        # imports were not working because it looks like these need to instantiate off
        # an object
        if version == 2:
            log_message = cnf.ControlPointFileEntryV0002().Measure().MeasureLogData()
        elif version == 5:
            log_message = cnp5.ControlPointFileEntryV0005().Measure().MeasureLogData()
        log_message.doubleDataValue = self.value
        log_message.doubleDataType = self.messagetype
        return log_message

    @classmethod
    def from_protobuf(cls, protobuf):
        return cls(protobuf.doubleDataType, protobuf.doubleDataValue)


class IsisControlNetwork(pd.DataFrame):

    # normal properties
    _metadata = ['header']

    @property
    def _constructor(self):
        return IsisControlNetwork

def from_isis(path, remove_empty=True):

    # Now get ready to work with the binary
    with IsisStore(path, mode='rb') as store:
        df = store.read()

    return df

def to_isis(obj, path, mode='wb', version=2,
            headerstartbyte=HEADERSTARTBYTE,
            networkid='None', targetname='None',
            description='None', username=DEFAULTUSERNAME,
            creation_date=None, modified_date=None,
            pointid_prefix=None, pointid_suffix=None):

    if targetname == 'None':
        warnings.warn("Users should provide a targetname to this function such as 'Moon' or 'Mars' in order to generate a valid ISIS control network.")

    with IsisStore(path, mode) as store:
        if not creation_date:
            creation_date = strftime("%Y-%m-%d %H:%M:%S", gmtime())
        if not modified_date:
            modified_date = strftime("%Y-%m-%d %H:%M:%S", gmtime())
        point_messages, point_sizes = store.create_points(obj, pointid_prefix, pointid_suffix)
        points_bytes = sum(point_sizes)
        buffer_header, buffer_header_size = store.create_buffer_header(networkid,
                                                                       targetname,
                                                                       description,
                                                                       username,
                                                                       point_sizes,
                                                                       creation_date,
                                                                       modified_date)
        # Write the buffer header
        store.write(buffer_header, HEADERSTARTBYTE)
        # Then write the points, so we know where to start writing, + 1 to avoid overwrite
        point_start_offset = HEADERSTARTBYTE + buffer_header_size
        for i, point in enumerate(point_messages):
            store.write(point, point_start_offset)
            point_start_offset += point_sizes[i]
        header = store.create_pvl_header(version, headerstartbyte, networkid,
                                         targetname, description, username,
                                         buffer_header_size, points_bytes,
                                         creation_date, modified_date)

        store.write(header.encode('utf-8'))

class IsisStore(object):
    """
    Class to manage IO of an ISIS control network (version 2).
    Attributes
    ----------
    pointid : int
              The current index to be assigned to newly added points
    """

    point_field_map = {
        'type' : 'pointType',
        'chooserName' : 'pointChoosername',
        'datetime' : 'pointDatetime',
        'editLock' : 'pointEditLock',
        'ignore' : 'pointIgnore',
        'jigsawRejected' : 'pointJigsawRejected',
        'log' : 'pointLog'
    }
    measure_field_map = {
        'type' : 'measureType',
        'choosername' : 'measureChoosername',
        'datetime' : 'measureDatetime',
        'editLock' : 'measureEditLock',
        'ignore' : 'measureIgnore',
        'jigsawRejected' : 'measureJigsawRejected',
        'log' : 'measureLog'
    }

    def __init__(self, path, mode=None, **kwargs):
        self.nmeasures = 0
        self.npoints = 0

        # Conversion from buffer types to Python types
        bt = {1: float,
              5: int,
              8: bool,
              9: str,
              11: list,
              14: int}
        self.header_attrs = [(i.name, bt[i.type]) for i in cnf._CONTROLNETFILEHEADERV0002.fields]
        self.point_attrs = [(i.name, bt[i.type]) for i in cnf._CONTROLPOINTFILEENTRYV0002.fields]
        self.measure_attrs = [(i.name, bt[i.type]) for i in cnf._CONTROLPOINTFILEENTRYV0002_MEASURE.fields]

        self._path = path
        if not mode:
            mode = 'a' # pragma: no cover
        self._mode = mode
        self._handle = None

        self._open()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, traceback):
        self.close()

    def close(self):
        if self._handle is not None:
            self._handle.close()
        self._handle = None

    def _open(self):
        self._handle = open(self._path, self._mode)

    def read(self):
        """
        Given an ISIS store, read the underlying ISIS3 compatible control network and
        return an IsisControlNetwork dataframe.
        """
        pvl_header = pvl.load(self._path, grammar=pvl.grammar.ISISGrammar())
        header_start_byte = find_in_dict(pvl_header, 'HeaderStartByte')
        header_bytes = find_in_dict(pvl_header, 'HeaderBytes')
        point_start_byte = find_in_dict(pvl_header, 'PointsStartByte')
        version = find_in_dict(pvl_header, 'Version')

        if version == 2:
            self.point_attrs = [i for i in cnf._CONTROLPOINTFILEENTRYV0002.fields_by_name if i != 'measures']
            self.measure_attrs = [i for i in cnf._CONTROLPOINTFILEENTRYV0002_MEASURE.fields_by_name]

            cp = cnf.ControlPointFileEntryV0002()
            self._handle.seek(header_start_byte)
            pbuf_header = cnf.ControlNetFileHeaderV0002()
            pbuf_header.ParseFromString(self._handle.read(header_bytes))

            self._handle.seek(point_start_byte)
            cp = cnf.ControlPointFileEntryV0002()
            pts = []
            for s in pbuf_header.pointMessageSizes:
                cp.ParseFromString(self._handle.read(s))
                pt = [getattr(cp, i) for i in self.point_attrs if i != 'measures']
                for measure in cp.measures:
                    meas = pt + [getattr(measure, j) for j in self.measure_attrs]
                    pts.append(meas)

        elif version == 5:
            self.point_attrs = [i for i in cnp5._CONTROLPOINTFILEENTRYV0005.fields_by_name if i != 'measures']
            self.measure_attrs = [i for i in cnp5._CONTROLPOINTFILEENTRYV0005_MEASURE.fields_by_name]

            cp = cnp5.ControlPointFileEntryV0005()
            self._handle.seek(header_start_byte)
            pbuf_header = cnh5.ControlNetFileHeaderV0005()
            pbuf_header.ParseFromString(self._handle.read(header_bytes))

            self._handle.seek(point_start_byte)
            cp = cnp5.ControlPointFileEntryV0005()
            pts = []
            byte_count = 0
            while byte_count < find_in_dict(pvl_header, 'PointsBytes'):
                message_size = struct.unpack('I', self._handle.read(4))[0]
                cp.ParseFromString(self._handle.read(message_size))
                pt = [getattr(cp, i) for i in self.point_attrs if i != 'measures']

                for measure in cp.measures:
                    meas = pt + [getattr(measure, j) for j in self.measure_attrs]
                    pts.append(meas)

                byte_count += 4 + message_size

        # Some point and measure fields have the same name, so mangle them as point_ and measure_
        point_cols = [self.point_field_map[attr] if attr in self.point_field_map else attr for attr in self.point_attrs]
        measure_cols = [self.measure_field_map[attr] if attr in self.measure_field_map else attr for attr in self.measure_attrs]
        cols = point_cols + measure_cols
        df = IsisControlNetwork(pts, columns=cols)
        # Convert the (0.5, 0.5) origin pixels back to (0,0) pixels
        df['line'] -= 0.5
        df['sample'] -= 0.5
        if 'aprioriline' in df.columns:
            df['aprioriline'] -= 0.5
            df['apriorisample'] -= 0.5

        # Munge the MeasureLogData into Python objs
        df['measureLog'] = df['measureLog'].apply(lambda x: [MeasureLog.from_protobuf(i) for i in x])

        df.header = pvl_header
        return df


    def write(self, data, offset=0):
        """
        Parameters
        ----------
        data : bytes
               Encoded header to be written to the file
        offset : int
                 The byte offset into the output binary
        """
        self._handle.seek(offset)
        self._handle.write(data)

    def create_points(self, df, pointid_prefix, pointid_suffix):
        """
        Step through a control network (C) and return protocol buffer point objects
        Parameters
        ----------
        df : DataFrame
              with the appropriate attributes: point_id, point_type, serial,
              measure_type, x, y required.
              The entries in the list must support grouping by the point_id attribute.
        Returns
        -------
        point_messages : list
                         of serialized points buffers
        point_sizes : list
                      of integer point sizes
        """
        def _set_pid(pointid):
            return '{}{}{}'.format(xstr(pointid_prefix),
                                   pointid,
                                   xstr(pointid_suffix))

        # TODO: Rewrite using apply syntax for performance
        point_sizes = []
        point_messages = []
        for i, g in df.groupby('id'):

            # Get the point specification from the protobuf
            point_spec = cnf.ControlPointFileEntryV0002()

            # Set refrence row to minimize .iloc calls and improve run time
            reference_row = g.iloc[0]

            # Set the ID and then loop over all of the attributes that the
            # point has and check for corresponding columns in the group and
            # set with the correct type
            #point_spec.id = _set_pid(i)
            point_spec.id = _set_pid(i)
            point_spec.type = reference_row.pointType
            try:
                point_spec.referenceIndex = reference_row.referenceIndex
            except:
                warnings.warn(f'Unable to identify referenceIndex for point {point_spec.id}. Defaulting to index 0.')
                point_spec.referenceIndex = 0
            for attr, attrtype in self.point_attrs:
                # Un-mangle common attribute names between points and measures
                df_attr = self.point_field_map.get(attr, attr)
                if df_attr in g.columns:
                    if df_attr == 'pointLog':
                        # Currently pointLog is not supported.
                        warnings.warn('The pointLog field is currently unsupported. Any pointLog data will not be saved.')
                        continue
                    # As per protobuf docs for assigning to a repeated field.
                    if df_attr == 'aprioriCovar' or df_attr == 'adjustedCovar':
                        arr = reference_row[df_attr]
                        if isinstance(arr, np.ndarray):
                            arr = arr.ravel().tolist()
                        if arr:
                            point_spec.aprioriCovar.extend(arr)
                    # If field is repeated you must extend instead of assign
                    elif cnf._CONTROLPOINTFILEENTRYV0002.fields_by_name[attr].label == 3:
                        getattr(point_spec, attr).extend(reference_row[df_attr])
                    else:
                        setattr(point_spec, attr, attrtype(reference_row[df_attr]))

            # A single extend call is cheaper than many add calls to pack points
            measure_iterable = []
            for node_id, m in g.iterrows():
                measure_spec = point_spec.Measure()
                # For all of the attributes, set if they are an dict accessible attr of the obj.
                for attr, attrtype in self.measure_attrs:
                    # Un-mangle common attribute names between points and measures
                    df_attr = self.measure_field_map.get(attr, attr)
                    if df_attr in g.columns:
                        if df_attr == 'measureLog':
                            [getattr(measure_spec, attr).extend([i.to_protobuf()]) for i in m[df_attr]]
                        # If field is repeated you must extend instead of assign
                        elif cnf._CONTROLPOINTFILEENTRYV0002_MEASURE.fields_by_name[attr].label == 3:
                            getattr(measure_spec, attr).extend(m[df_attr])
                        else:
                            setattr(measure_spec, attr, attrtype(m[df_attr]))
                # ISIS pixels are centered on (0.5, 0.5). NDArrays are (0,0) based.
                measure_spec.sample = m['sample'] + 0.5
                measure_spec.line = m['line'] + 0.5
                if 'apriorisample' in g.columns:
                    measure_spec.apriorisample = m['apriorisample'] + 0.5
                    measure_spec.aprioriline = m['aprioriline'] + 0.5
                measure_iterable.append(measure_spec)
                self.nmeasures += 1

            self.npoints += 1

            point_spec.measures.extend(measure_iterable)
            point_message = point_spec.SerializeToString()
            point_sizes.append(point_spec.ByteSize())
            point_messages.append(point_message)
        return point_messages, point_sizes

    def create_buffer_header(self, networkid, targetname,
                             description, username, point_sizes,
                             creation_date,
                             modified_date):
        """
        Create the Google Protocol Buffer header using the
        protobuf spec.
        Parameters
        ----------
        networkid : str
                    The user defined identifier of this control network
        targetname : str
                 The name of the target, e.g. Moon
        description : str
                  A description for the network.
        username : str
               The name of the user / application that created the control network
        point_sizes : list
                      of the point sizes for each point message
        Returns
        -------
        header_message : str
                  The serialized message to write
        header_message_size : int
                              The size of the serialized header, in bytes
        """
        raw_header_message = cnf.ControlNetFileHeaderV0002()
        raw_header_message.created = creation_date
        raw_header_message.lastModified = modified_date
        raw_header_message.networkId = networkid
        raw_header_message.description = description
        raw_header_message.targetName = targetname
        raw_header_message.userName = username
        raw_header_message.pointMessageSizes.extend(point_sizes)

        header_message_size = raw_header_message.ByteSize()
        header_message = raw_header_message.SerializeToString()

        return header_message, header_message_size

    def create_pvl_header(self, version, headerstartbyte,
                      networkid, targetname, description, username,
                          buffer_header_size, points_bytes,
                          creation_date, modified_date):
        """
        Create the PVL header object
        Parameters
        ----------
        version : int
              The current ISIS version to write, defaults to 2
        headerstartbyte : int
                          The seek offset that the protocol buffer header starts at
        networkid : str
                    The name of the network
        targetname : str
                     The name of the target, e.g. Moon
        description : str
                      A description for the network.
        username : str
                   The name of the user / application that created the control network
        buffer_header_size : int
                             Total size of the header in bytes
        points_bytes : int
                       The total number of bytes all points require
        Returns
        -------
         : object
           An ISIS compliant PVL header object
        """

        encoder = pvl.encoder.ISISEncoder(end_delimiter=False)

        header_bytes = buffer_header_size
        points_start_byte = HEADERSTARTBYTE + buffer_header_size

        header = pvl.PVLModule([
            ('ProtoBuffer',
                ({'Core':{'HeaderStartByte': headerstartbyte,
                        'HeaderBytes': header_bytes,
                        'PointsStartByte': points_start_byte,
                        'PointsBytes': points_bytes},

                  'ControlNetworkInfo': pvl.PVLGroup([
                        ('NetworkId', networkid),
                        ('TargetName', targetname),
                        ('UserName', username),
                        ('Created', creation_date),
                        ('LastModified', modified_date),
                        ('Description', description),
                        ('NumberOfPoints', self.npoints),
                        ('NumberOfMeasures', self.nmeasures),
                        ('Version', version)
                        ])
                  }),

                 )
        ])

        return pvl.dumps(header, encoder=encoder)
