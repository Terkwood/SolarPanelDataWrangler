import os
import pathlib
import time
from io import BytesIO

from PIL import Image
from mapbox import Static

import solardb
from process_city_shapes import num2deg


class ImageTile(object):
    """Represents a single image tile."""

    def __init__(self, image, coords, filename=None):
        self.image = image
        self.coords = coords
        self.filename = filename

    @property
    def column(self):
        return self.coords[0]

    @property
    def row(self):
        return self.coords[1]

    @property
    def basename(self):
        """Strip path and extension. Return base filename."""
        return get_basename(self.filename)

    def generate_filename(self, zoom=21, directory=os.path.join(os.getcwd(), 'data', 'imagery'),
                          format='jpg', path=True):
        """Construct and return a filename for this tile."""
        filename = os.path.join(str(zoom), str(self.row), str(self.column) +
                                '.{ext}'.format(ext=format.lower().replace('jpeg', 'jpg')))
        if not path:
            return filename
        return os.path.join(directory, filename)

    def save(self, filename=None, file_format='jpeg', zoom=21):
        if not filename:
            filename = self.generate_filename(zoom=zoom)
        pathlib.Path(os.path.dirname(filename)).mkdir(parents=True, exist_ok=True)
        self.image.save(filename, file_format)
        self.filename = filename

    def load(self, filename=None, zoom=21):
        if not filename:
            filename = self.generate_filename(zoom=zoom)
        if self.image:
            return self.image
        if not pathlib.Path(filename).is_file():
            return None
        self.filename = filename
        self.image = Image.open(filename)
        return self.image

    def delete(self, filename=None, zoom=21):  # TODO zoom should probably be a tile property
        if not filename:
            filename = self.generate_filename(zoom=zoom)
        pathlib.Path(filename).unlink()
        self.filename = filename

    def __repr__(self):
        """Show tile coords, and if saved to disk, filename."""
        if self.filename:
            return '<Tile #{} - {}>'.format(self.coords,
                                            self.filename)
        return '<Tile #{}>'.format(self.coords)


def get_basename(filename):
    """Strip path and extension. Return basename."""
    return os.path.splitext(os.path.basename(filename))[0]


# assumes a square image
def slice_image(image, base_coords, upsample_count=0, slices_per_side=5):
    out = image
    base_column, base_row = base_coords
    for i in range(upsample_count):
        out = double_image_size(out)
    w, h = out.size
    w = w // slices_per_side
    h = h // slices_per_side
    tiles = []
    for row_offset in range(slices_per_side):
        for column_offset in range(slices_per_side):
            box = (column_offset * w, row_offset * h, (column_offset + 1) * w, (row_offset + 1) * h)
            cropped_image = out.crop(box)
            coords = (column_offset + base_column, row_offset + base_row)
            tiles.append(ImageTile(cropped_image, coords))
    return tiles


def double_image_size(image, filter=Image.LANCZOS):
    return image.resize((image.size[0] * 2, image.size[0] * 2), filter)


# amount of zooms out to do from final zoom level when querying for imagery
ZOOM_FACTOR = 2
# the final zoom level of the saved tiles
FINAL_ZOOM = 21
TILE_SIDE_LENGTH = 256
MAX_IMAGE_SIDE_LENGTH = 1280
# number of times to cut the source imagery to get it to correct tile size
GRID_SIZE = (MAX_IMAGE_SIDE_LENGTH // TILE_SIDE_LENGTH) * 2 ** ZOOM_FACTOR

# the side length of the stitched image
FINISHED_TILE_SIDE_LENGTH = 320
# the amount to stitch from each border tile
STITCH_WIDTH = (FINISHED_TILE_SIDE_LENGTH - TILE_SIDE_LENGTH) // 2
# the amount not to stitch from each border tile
CROPPED_WIDTH = TILE_SIDE_LENGTH - STITCH_WIDTH
CROP_BOXES = [
    (CROPPED_WIDTH, CROPPED_WIDTH, TILE_SIDE_LENGTH, TILE_SIDE_LENGTH),
    (CROPPED_WIDTH, 0, TILE_SIDE_LENGTH, TILE_SIDE_LENGTH),
    (CROPPED_WIDTH, 0, TILE_SIDE_LENGTH, STITCH_WIDTH),
    (0, CROPPED_WIDTH, TILE_SIDE_LENGTH, TILE_SIDE_LENGTH),
    (0, 0, TILE_SIDE_LENGTH, TILE_SIDE_LENGTH),
    (0, 0, TILE_SIDE_LENGTH, STITCH_WIDTH),
    (0, CROPPED_WIDTH, STITCH_WIDTH, TILE_SIDE_LENGTH),
    (0, 0, STITCH_WIDTH, TILE_SIDE_LENGTH),
    (0, 0, STITCH_WIDTH, STITCH_WIDTH)
]
PASTE_COORDINATES = [
    (0, 0),
    (0, STITCH_WIDTH),
    (0, TILE_SIDE_LENGTH + STITCH_WIDTH),
    (STITCH_WIDTH, 0),
    (STITCH_WIDTH, STITCH_WIDTH),
    (STITCH_WIDTH, TILE_SIDE_LENGTH + STITCH_WIDTH),
    (TILE_SIDE_LENGTH + STITCH_WIDTH, 0),
    (TILE_SIDE_LENGTH + STITCH_WIDTH, STITCH_WIDTH),
    (TILE_SIDE_LENGTH + STITCH_WIDTH, TILE_SIDE_LENGTH + STITCH_WIDTH),
]

MAX_RETRIES = 12  # max wait time with exponential backoff would be ~34 minutes

service = Static()


def gather_and_persist_imagery_at_coordinate(slippy_coordinates, final_zoom=FINAL_ZOOM, grid_size=GRID_SIZE,
                                             imagery="mapbox"):
    # the top left square of the query grid this point belongs to
    base_coords = tuple(map(lambda x: x - x % grid_size, slippy_coordinates))
    if grid_size % 2 == 0:
        # if the grid size is even, the center point is between 4 tiles in center (or the top left of bottom right one)
        center_bottom_right_tile = tuple(map(lambda x: x + grid_size // 2, base_coords))
        center_lon_lat = num2deg(center_bottom_right_tile, zoom=final_zoom, center=False)
    else:
        # if the grid is odd, the center point is in the center of the center square
        center_tile = tuple(map(lambda x: x + grid_size // 2, base_coords))
        center_lon_lat = num2deg(center_tile, zoom=FINAL_ZOOM, center=True)
    if imagery == "mapbox":
        for i in range(MAX_RETRIES):
            response = service.image('mapbox.satellite', lon=center_lon_lat[0], lat=center_lon_lat[1], z=final_zoom - 2,
                                     width=MAX_IMAGE_SIDE_LENGTH, height=MAX_IMAGE_SIDE_LENGTH, image_format='jpg90',
                                     retina=(ZOOM_FACTOR > 0))
            if response.ok:
                image = Image.open(BytesIO(response.content))
                tiles = slice_image(image, base_coords, upsample_count=max(ZOOM_FACTOR - 1, 0),
                                    slices_per_side=grid_size)
                to_return = None
                for tile in tiles:
                    if tile.coords == slippy_coordinates:
                        to_return = tile.image
                    tile.save(zoom=FINAL_ZOOM)
                solardb.mark_has_imagery(base_coords, grid_size, zoom=final_zoom)
                return to_return
            backoff_time = pow(2, i)
            print('Got this response from {service}:"{error}", exponentially backing off, {time} seconds.'
                  .format(service=imagery, error=getattr(response, "content", None), time=backoff_time))
            time.sleep(backoff_time)
        raise ConnectionError("Couldn't connect to {service} after {retries}"
                              .format(service=imagery, retries=MAX_RETRIES))
    else:
        AttributeError("Unsupported Imagery source: " + str(imagery))


# loads image from disk if possible, otherwise queries an imagery service
def get_image_for_coordinate(slippy_coordinate):
    tile = ImageTile(None, slippy_coordinate)
    image = tile.load()
    if not image:
        image = gather_and_persist_imagery_at_coordinate(slippy_coordinate, final_zoom=FINAL_ZOOM)
    return image


# gets a larger image at the specified slippy coordinate by stitching other border tiles together
# TODO: optimize
# TODO: there's also some symmetry here that can be exploited but this seems easier for now
def stitch_image_at_coordinate(slippy_coordinate):
    images = []
    # gather the images in each direction around the target image
    for column in range(slippy_coordinate[0] - 1, slippy_coordinate[0] + 2):
        for row in range(slippy_coordinate[1] - 1, slippy_coordinate[1] + 2):
            images.append(get_image_for_coordinate((column, row),))

    cropped_images = []
    for image, crop_box in zip(images, CROP_BOXES):
        cropped_images.append(image.crop(crop_box))
    output_image = Image.new('RGB', (FINISHED_TILE_SIDE_LENGTH, FINISHED_TILE_SIDE_LENGTH))
    for cropped_image, paste_coordinate in zip(cropped_images, PASTE_COORDINATES):
        cropped_images.append(output_image.paste(cropped_image, box=paste_coordinate))
    return output_image


def delete_images(slippy_coordinates):
    for coordinate_tuple in slippy_coordinates:
        ImageTile(None, coordinate_tuple).delete(zoom=coordinate_tuple[2])
