#!/usr/bin/env python3
"""
Tools for analysing forest point clouds.

Inputs: a coloured pointcloud in ``.ply`` format (XYZRGB vertices), which
can be obtained by putting images from drone photography through
structure-from-motion software.
Specifically: little-endian binary format ply

Outputs (most are optional):

* A 'sparse' (i.e. canopy-only) point cloud, with most ground points discarded.
  This eases further analysis, storage, etc without compromising coverage of vegetation.
* A ``.csv`` file listing identified trees, with traits including location,
  height, canopy area, colour, and point count.
* Individual files containing the point cloud for each tree.

Extensive use of mutable coordinate-property mappings and streamed input
ensure that even files too large to load in memory can be processed.  In extreme
cases, the resolution can be decreased to trade accuracy for memory.

Example outputs (from an older version):
`a map <https://www.google.com/maps/d/viewer?mid=z1pH7HaTWL9Q.kzQflQGYVRIU>`_,
and `pointclouds <http://phenocam.org.au/pointclouds>`_.
"""
# pylint:disable=unsubscriptable-object

# log file name base - the log file will be this + the current time + '.log'
LOG_NAME = 'forestutils'

import argparse
import csv
import math
import os
import datetime
import logging
from typing import MutableMapping, NamedTuple, Tuple, Set

import utm  # type: ignore

from . import pointcloudfile


# User-defined types
#XY_Coord is
XY_Coord = NamedTuple('XY_Coord', [('x', int), ('y', int)])
Coord_Labels = MutableMapping[XY_Coord, int]


def coords(pos):
    """
    Return a tuple of integer coordinates as keys for the MapObj dict/map.
    This is necessary because the MapObj uses a dictionary to store each
    attribute.
    * pos can be a full point tuple, or just (x, y)
    * use floor() to avoid imprecise float issues
    """
    x = math.floor(pos.x / args.cellsize)
    y = math.floor(pos.y / args.cellsize)
    return XY_Coord(x, y)


def neighbors(key: XY_Coord) -> Tuple[XY_Coord, ...]:
    """
    Take an XY coordinate key and return the adjacent keys,
	whether they exist or not.
    """
    return tuple(XY_Coord(key.x + a, key.y + b)
                 for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b)


def connected_components(input_dict: Coord_Labels) -> None:
    """
    Connected components in a dict of coordinates.
    Uses depth-first search.  Non-component cells are absent from the input.
    """
    def expand(old_key: XY_Coord, com: MutableMapping) -> None:
        """
        Implement depth-first search.
		"""
        for key in neighbors(old_key):
            if com.get(key) is None:
                return
            if com[key] == com[old_key]:
                continue
            elif com[key] < com[old_key]:
                com[old_key] = com[key]
            else:
                com[key] = com[old_key]
                expand(key, com)

    for key in tuple(input_dict):
        try:
            expand(key, input_dict)
        except RuntimeError:
            logging.info('Maximum recursion depth exceeded; finishing run.')
            # Recursion depth; finish on next pass.
            continue


def detect_issues(ground_dict: Coord_Labels, prior: set) -> Set[XY_Coord]:
    """
    Identifies cells with more than 2:1 slope to 3+ adjacent cells.
    Greater than 2:1 slope is suspiciously steep; 3+ usually indicates a
    misclassified cell or data artefact.
    """
    problematic = set()
    for k in prior:
        adjacent = {ground_dict.get(n) for n in neighbors(k)}
        adjacent.discard(None)
        if len(adjacent) < 6:
            continue
        # Number of cells at more than 2:1 slope - suspiciously steep.
        # 3+ usually indicates a misclassified cell or data artefact.
        probs = sum(abs(ground_dict[k]-n) > 2*args.cellsize for n in adjacent)
        if probs >= 3:
            problematic.add(k)
    return problematic


def smooth_ground(ground_dict: Coord_Labels) -> None:
    """
    Smoothes the ground map, to reduce the impact of spurious points, eg.
    points far underground or misclassification of canopy as ground.
    """
    logging.info('Smoothing the ground map.')
    problematic = set(ground_dict)
    for _ in range(100):
        problematic = detect_issues(ground_dict, problematic)
        for key in problematic:
            adjacent = {ground_dict.get(n) for n in neighbors(key)
                        if n not in problematic}
            adjacent.discard(None)
            if not adjacent:
                continue
            ground_dict[key] = min(adjacent) + 2*args.cellsize


class MapObj:
    """
    Stores a maximum and minimum height map of the cloud, in GRID_SIZE
    cells.  Hides data structure and accessed through coordinates.
    Data structure is a set of dictionaries, one for each attribute. Each dict
    is a contains, for a single attribute, all the values for all the points.
    Note that the dict is faster to access than an array.
    """
    # pylint:disable=too-many-instance-attributes

    def __init__(self, input_file, *, colours=True):
        """
        Args:
            input_file (path): the ``.ply`` file to process.  If dealing with
                Pix4D outputs, ``*_part_1.ply``.
            colours (bool): whether to read colours from the file.  Set to
                False for eg. LIDAR data where mean colour is not useful.
            prev_csv (path): path to a csv file which associates a name
                with coordinates, to correctly name detected trees.
            zone (int): the UTM zone of the site.
            south (bool): if the site is in the southern hemisphere.
        """
        logging.debug('Create a MapObj')
        self.file = input_file
        self.canopy = dict()
        self.density = dict()
        self.filtered_density = dict()
        self.ground = dict()
        self.colours = dict()
        self.trees = dict()

        self.header = pointcloudfile.parse_ply_header(
            pointcloudfile.ply_header_text(input_file))
        logging.info('Moving x,y by utm offset by calling pointcloudfile.offset_for({})'.format(input_file))
        x, y, _ = pointcloudfile.offset_for(input_file)
        self.utm = pointcloudfile.UTM_Coord(x, y, args.utmzone, args.north)

        self.update_spatial()
        if colours:
            self.update_colours()

    def update_spatial(self):
        """
        Expand, correct, or maintain map with a new observed point.
		Initialize density and filtered_density to 1.
        Increment density but do not increment filtered_density - that is done
        in function update_colors
        """
        # Fill out the spatial info in the file
        for p in pointcloudfile.read(self.file):
            idx = coords(p)
            if self.density.get(idx) is None:
                self.density[idx] = 1
                self.canopy[idx] = p.z
                self.ground[idx] = p.z
                self.filtered_density[idx] = 1
                continue
            self.density[idx] += 1
            if self.ground[idx] > p.z:
                self.ground[idx] = p.z
            elif self.canopy[idx] < p.z:
                self.canopy[idx] = p.z
        smooth_ground(self.ground)
        self.trees = self._tree_components()

    def update_colours(self):
        """
        Expand, correct, or maintain map with a new observed point.
        """
        # We assume that vertex attributes not named "x", "y" or "z"
        # are colours, and thus accumulate a total to get the mean
        for p in pointcloudfile.read(self.file):
            if self.is_ground(p):
                continue
            p_cols = {k: v for k, v in p._asdict().items() if k not in 'xyz'}
            idx = coords(p)
            # filtered_density is the total number of points in the tree after
            # the ground has been removed
            self.filtered_density[idx] += 1
            if idx not in self.colours:
                self.colours[idx] = p_cols
            else:
                for k, v in p_cols.items():
                    self.colours[idx][k] += v

    def is_ground(self, point) -> bool:
        """
        Returns boolean whether the point is not classified as ground - i.e.
        True if within GROUND_DEPTH of the lowest point in the cell.
        If not lossy, also true for lowest ground point in a cell.
        """
        return point[2] - self.ground[coords(point)] < args.grounddepth

    def is_lowest(self, point) -> bool:
        """Returns boolean whether the point is lowest in that grid cell.
        """
        return point[2] == self.ground[coords(point)]

    def __len__(self) -> int:
        """Total observed points.
        """
        return sum(self.density.values())

    def _tree_components(self) -> Coord_Labels:
        """Returns a dict where keys refer to connected components.
        NB: Not all keys in other dicts exist in this output.
        """
        # Set up a boolean array of larger keys to search
        key_scale_record = {}  # type: Dict[XY_Coord, Set[XY_Coord]]
        for key in self.density:
            if self.canopy[key] - self.ground[key] > args.slicedepth:
                cc_key = XY_Coord(int(math.floor(key.x / args.joinedcells)),
                                  int(math.floor(key.y / args.joinedcells)))
                if cc_key not in key_scale_record:
                    key_scale_record[cc_key] = {key}
                else:
                    key_scale_record[cc_key].add(key)
        # Assign a unique integer value to each large key, then search
        # Final labels are positive ints, but not ordered or consecutive
        trees = {k: i for i, k in enumerate(tuple(key_scale_record))}
        connected_components(trees)
        # Copy labels to grid of original scale
        return {s: trees[k] for k, v in key_scale_record.items() for s in v}

    def tree_data(self, keys: Set[XY_Coord]) -> dict:
        """
        Return a dictionary of data about the tree in the given keys.
        """
        # Calculate positional information
        x = self.utm.x + args.cellsize * sum(k.x for k in keys) / len(keys)
        y = self.utm.y + args.cellsize * sum(k.y for k in keys) / len(keys)
        lat, lon = utm.to_latlon(x, y, self.utm.zone, northern=self.utm.north)
        out = {
            'latitude': lat,
            'longitude': lon,
            'UTM_X': x,
            'UTM_Y': y,
            'UTM_zone': args.utmzone,
            'height': 0,
            'area': len(keys) * args.cellsize**2,
            'base_altitude': sum(self.ground[k] for k in keys) / len(keys),
            'point_count': 0,
            }
        for k in keys:
            out['height'] = max(out['height'], self.canopy[k] - self.ground[k])
            out['point_count'] += self.density[k]
            for colour, total in self.colours[k].items():
                out[colour] = total / self.filtered_density[k]
        return out

    def all_trees(self):
        """
        Yield the characteristics of each tree.
        Use to iterate over the trees. 
        """
        ids = list(set(self.trees.values()))
        keys = {v: set() for v in ids}
        for k, v in self.trees.items():
            if v is None:
                continue
            keys[v].add(k)
        for v in ids:
            data = self.tree_data(keys[v])
            if data['height'] > 1.5 * args.slicedepth:
                # Filter trees by height
                yield data

    def save_sparse_cloud(self, new_fname, lowest=True, canopy=True):
        """
        Yield points for a canopy-only point cloud, eliminating ~3/4 of all
        points without affecting analysis.
        """
        newpoints = (point for point in pointcloudfile.read(self.file)
                     if canopy and not self.is_ground(point) or
                     lowest and self.is_lowest(point))
        pointcloudfile.write(newpoints, new_fname, self.header, self.utm)
        if lowest and canopy:
            self.file = new_fname

    def save_individual_trees(self):
        """
        Save single trees to pointcloud files, if the 'savetrees' flag is set.
        Use the directory specified by the savetrees flag.
        """
        if not args.savetrees:
            return
        if os.path.isfile(args.savetrees):
            error = 'Output dir for trees is a file; a directory is required.'
            logging.error(error)
            raise IOError(error)
        if not os.path.isdir(args.savetrees):
            os.makedirs(args.savetrees)
        # Map tree ID numbers to an incremental writer for that tree
        tree_to_file = {tree_ID: pointcloudfile.IncrementalWriter(
            os.path.join(args.savetrees, 'tree_{}.ply'.format(tree_ID)),
            self.header, self.utm) for tree_ID in set(self.trees.values())}
        # For non-ground, find the appropriate writer and call with the point
        for point in pointcloudfile.read(self.file):
            val = self.trees.get(coords(point))
            if val is not None:
                tree_to_file[val](point)

    def stream_analysis(self, csv_filename: str) -> None:
        """
        Save the list of trees with attributes to the file in file 'csv_filename'.
        """
        logging.info('Write the tree data to the csv file "{}"'.format(csv_filename))
        header = ('latitude', 'longitude', 'UTM_X', 'UTM_Y', 'UTM_zone',
                  'height', 'area', 'base_altitude', 'point_count') + tuple(
                      a for a in self.header.names if a not in 'xyz')
        with open(csv_filename, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=header)
            writer.writeheader()
            for data in self.all_trees():
                writer.writerow(data)


def get_args():
    """
    Handle command-line arguments, including default values.
    """
    parser = argparse.ArgumentParser(
        description=('Takes a .ply forest  point cloud; outputs a sparse'
                     'point cloud and a .csv file of attributes'
                     'for each tree.'))
    parser.add_argument(
        'file', help='name of the file to process', type=str)
    parser.add_argument(
        'out', default='.', nargs='?', type=str,
        help='directory for output files (optional)')
    parser.add_argument(
        '--savetrees', default='', nargs='?', type=str,
        help='where to save individual trees (default "", not saved)')
    parser.add_argument(  # analysis scale
        '--cellsize', default=0.1, nargs='?', type=float,
        help='grid scale; optimal at ~10x point spacing')
    parser.add_argument(  # georeferenced location
        '--utmzone', default=55, type=int,
        help='the UTM coordinate zone for georeferencing')
    parser.add_argument(  # georeferenced location
        '--north', action='store_true',
        help='set if in the northern hemisphere')
    parser.add_argument(  # feature extraction
        '--joinedcells', default=3, type=float,
        help='use cells X times larger to detect gaps between trees')
    parser.add_argument(  # feature extraction
        '--slicedepth', default=0.6, type=float,
        help='slice depth for canopy area and feature extraction')
    parser.add_argument(  # feature classification
        '--grounddepth', default=0.2, type=float,
        help='depth to omit from sparse point cloud')
    return parser.parse_args()


def main_processing():
    """
    Logic on which functions to call, and efficient order.

    """
    # args is a global variable
    print('Reading from "{}" ...'.format(args.file))
    logging.info('Reading from "{}" ...'.format(args.file))

    # File I/O

    # sparse_filename is a string containing the name of the main output file
    # Set output file name to <input file name>_sparse.ply
    sparse_filename = os.path.join(args.out, os.path.basename(args.file))
    if not args.file.endswith('_sparse.ply'):
        sparse_filename = os.path.join(
            args.out, os.path.basename(args.file)[:-4] + '_sparse.ply')
    sparse_filename = sparse_filename.replace('_part_1', '')

    """
    Confirm why this is done - I am re-running this, so sparse already exists. But when
    we create the object using the sparse filename, then the .xyz filename is wrong.
    Confirm what point of this was??
    """
    if os.path.isfile(sparse_filename):
        logging.info('"sparse" file already exist, using this file')
        attr_map = MapObj(sparse_filename)
        print('Read {} points into {} cells'.format(
            len(attr_map), len(attr_map.canopy)))
        logging.info('Read {} points into {} cells'.format(
            len(attr_map), len(attr_map.canopy)))
    else:
        attr_map = MapObj(args.file, colours=False)
        print('Read {} points into {} cells, writing "{}" ...'.format(
            len(attr_map), len(attr_map.canopy), sparse_filename))
        logging.info('Read {} points into {} cells, writing "{}" ...'.format(
            len(attr_map), len(attr_map.canopy), sparse_filename))
        attr_map.save_sparse_cloud(sparse_filename)
        print('Reading colours from ' + sparse_filename)
        logging.info('Reading colours from {}'.format(sparse_filename))
        attr_map.update_colours()
    print('File IO complete, starting analysis...')
    logging.info('File IO complete, starting analysis...')

    # table is a string containing the name of the csv file to save tree data in
    table = '{}_analysis.csv'.format(sparse_filename[:-4].replace('_sparse', ''))
    # write the tree data to a csv file
    logging.info('Calling stream_analysis to write the csv file')
    attr_map.stream_analysis(table)

    # save pointclouds for individual trees
    if args.savetrees is not None:
        print('Saving individual trees...')
        logging.info('Saving individual trees')
        attr_map.save_individual_trees()
    print('Done.')
    logging.info('Done.')

def logging_setup():
    """
    Set up an execution log and set the format for the log records.

    """
    # get the current time, and create a log file name using the time
    starttime = str(datetime.datetime.now())
    print(starttime)
    # format the time string to something filename friendly (no ':')
    # and remove milliseconds (after .) and seconds (last 3 chars)
    starttime = starttime.split(sep='.')[0].replace(':','-')[:-3]
    logfilename = LOG_NAME + '-' + starttime + '.log'

    # create and configure log
    # change the level to logging.DEBUG to see all the debugging messages
    logging.basicConfig(
        filename=logfilename, level=logging.DEBUG,
        format='%(asctime)s %(levelname)s - %(funcName)s: %(message)s',
        datefmt="%Y-%m-%d %H:%M"
        )
    logging.info('Started forestutils.')
    logging.debug(' logging_setup: created a log file')

def main():
    """
    Interface to call from outside the package.
    """
    # pylint:disable=global-statement

    print('Welcome to forestutils 3D tree mapping program.')

    # start an execution log for the program for info and/or debugging
    logging_setup()

    global args
    args = get_args()

    # perform IO checks to ensure that:
    # - the input file exists
    # - if given, the output dir exists and is a directory (not a file)
    # - if the savetrees flag is set, it specifies a valid directory
    if not os.path.isfile(args.file):
        logging.error('Input file not found.')
        raise IOError('Input file not found, ' + args.file)
    # Check that 'out' is a valid folder now, BEFORE doing all the processing
    if not os.path.isdir(args.out):
        logging.error('Output directory is not valid.')
        raise IOError('Output directory is not valid, ' + args.out)
    # If savetrees flag is set, check if there is a dir specified which
    # already exists but is a file, and if so raise an error now
    if args.savetrees is not None:
        if os.path.isfile(args.savetrees):
            logging.error('Output dir for trees is a file; a directory is required.')
            raise IOError('Output dir for trees is a file; a directory is required.')

    logging.info('Commencing main processing function.')
    main_processing()

if __name__ == '__main__':
    # Call to get_args is duplicated to work in static analysis, from
    # command line, and when installed as package (calls main directly)
    args = get_args()
    main()
