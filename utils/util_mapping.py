import descarteslabs as dl
import numpy as np
import os
import gdal
import time

from image_generator import ImageGenerator
import utils.util_rasters as util_rasters
import utils.util_imagery as util_imagery

def make_water_mask_tile(data_path, place, tile_id, tiles, image_suffix, threshold):
    assert type(tile_id) is int 
    assert tile_id < len(tiles['features'])
    tile = tiles['features'][tile_id]
    resolution = int(tile['properties']['resolution'])

    #print('tile', tile_id, 'load VIR image')
    # assert resolution==10 or resolution==5
    if resolution==10:
        zfill = 3
    elif resolution==5:
        zfill = 4
    elif resolution==2:
        zfill=5
    else:
        raise Exception('bad resolution: '+str(resolution))
    vir_file = data_path+place+'_tile'+str(tile_id).zfill(zfill)+'_vir_'+image_suffix+('' if resolution==10 else '_'+str(resolution)+'m')+'.tif'

    #print(vir_file)
    vir, virgeo, virprj, vircols, virrows = util_rasters.load_geotiff(vir_file,dtype='uint16')
    #print('vir shape:',vir.shape)

    water = util_imagery.calc_water_mask(vir[0:6], threshold=threshold, bands_first=True)
    water_file = data_path+place+'_tile'+str(tile_id).zfill(zfill)+'_water_'+image_suffix+'.tif'
    print(water_file)
    util_rasters.write_1band_geotiff(water_file, water, virgeo, virprj, data_type=gdal.GDT_Byte)
    return water


# CLOUDS

def calc_cloud_score_default(clouds_window):
    assert len(clouds_window.shape)==2
    n_pixels = clouds_window.shape[0] * clouds_window.shape[1] * 1.0
    rating_sum = np.sum(clouds_window)
    return rating_sum / n_pixels

def map_cloud_scores(clouds, look_window, scorer=calc_cloud_score_default, pad=32):
    assert len(clouds.shape)==2
    assert clouds.shape[0]==clouds.shape[1]
    rows = clouds.shape[0]
    cols = clouds.shape[1]
    r = math.floor(look_window / 2)
    score_map = np.zeros(clouds.shape, dtype='float32')
    for j in range(rows):
        for i in range(cols):
            
            clouds_window = clouds[j-r:j+r+1,i-r:i+r+1]
#             if clouds_window.shape!=(look_window, look_window):
#                 print 'bad shape at '+str(j)+','+str(i)+': '+str(clouds_window.shape)
            window_score = scorer(clouds_window)
            score_map[j,i] = window_score
    if pad is not None:
        score_map[:pad,:] = -1.0; score_map[-pad:,:] = -1.0
        score_map[:,:pad] = -1.0; score_map[:,-pad:] = -1.0
    return score_map

def cloudscore_image(im, look_window,
                    tile_pad=32,
                    bands_first=True
                    ):
    image = util_imagery.s2_preprocess(im, bands_first=bands_first)
    Y, key = util_imagery.s2_cloud_mask(image,get_rgb=False,bands_first=bands_first)
    cloud_mask = (Y==4)
    cloud_scores = map_cloud_scores(cloud_mask, look_window, pad=tile_pad)
    return cloud_mask, cloud_scores

def map_tile(dl_id, tile, tile_id, network,
                    read_local=False,
                    write_local=True,
                    store_predictions=False,
                    make_watermask=True,
                    store_cloudmask=False,
                    store_watermask=False,
                    bands=['blue','green','red','nir','swir1','swir2','alpha'],
                    resampler='bilinear',
                    #cutline=shape['geometry'], #cut or no?
                    processing_level=None,
                    window=17,
                    data_root='/data/phase_iv/',
                    zfill=4
                    ):
    dl_id = str(dl_id)
    dl_id_short = dl_id[str.rfind(dl_id,':')+1:]
    tile_size = tile['properties']['tilesize']
    tile_pad = tile['properties']['pad']
    tile_res = int(tile['properties']['resolution'])
    tile_side = tile_size+(2*tile_pad)

    if read_local: # read file on local machine
        pass
        # tilepath = data_root+place+'/imagery/'+str(processing_level).lower()+'/'+\
        #     place+'_'+source+'_'+image_suffix+'_'+str(resolution)+'m'+'_'+'p'+str(tile_pad)+'_'+\
        #     'tile'+str(tile_id).zfill(zfill)+'.tif'

        #if loaded from file via gdal, will be bands_first, so swap
        #im = im.swapaxes(0,1).swapaxes(1,2)
    else: # read from dl
        im, metadata = dl.raster.ndarray(
            dl_id,
            bands=bands,
            resampler=resampler,
            data_type='UInt16',
            #cutline=shape['geometry'], 
            order='image', # 'image' returns arrays as (row, column, band) while 'gdal' returns arrays as (band, row, column)
            dltile=tile,
            processing_level=processing_level,
            )
    dl_id = str(dl_id)

    # insert test here for if imagery is empty: how else to account for 'empty' tiles?
    # will become more important/challenging as we move to generalized imagery
    # on the other hand, maybe without a cutline this isn't an issue.
    # placeholder comment for now, let's see how the application context develops
    
    alpha_mask = im[:,:,-1].astype(bool)
    if np.sum(alpha_mask) == 0:
        return
    
#     print('non-empty tile: #', tile_id)
    blank_mask = np.invert(alpha_mask)
    # create cloudscore from image
    cloud_mask, cloud_scores = cloudscore_image(im, window, tile_pad=tile_pad, bands_first=False)
    # classify image using nn
    generator = ImageSampleGenerator(im,pad=tile_pad,look_window=17,prep_image=True, bands_last=True)
    
    
    predictions = network.predict_generator(generator, steps=generator.steps, verbose=0,
        use_multiprocessing=False, max_queue_size=1, workers=1,)
    print (predictions.shape)
    
    if store_predictions:
        pred_square = predictions.reshape((tile_size,tile_size,3),order='F')
        pred = np.zeros((tile_side,tile_side,3),dtype='float32')
        pred.fill(255)
        pred[tile_pad:-tile_pad,tile_pad:-tile_pad,:] = pred_square[:,:,:]
        pred[blank_mask] = 255
        pred = pred.swapaxes(0,2)
        pred = pred.swapaxes(1,2)
 
    
    Yhat = predictions.argmax(axis=-1)
    Yhat_square = Yhat.reshape((tile_size,tile_size),order='F')
    lulc = np.zeros((tile_side,tile_side),dtype='uint8')
    lulc.fill(255)
    lulc[tile_pad:-tile_pad,tile_pad:-tile_pad] = Yhat_square[:,:]
    lulc[blank_mask]=255
    # -> store output
    if make_watermask:
        water_mask = util_imagery.calc_water_mask(im[:,:,:-1], bands_first=False)
        water_mask[blank_mask] = 255
    else:
        water_mask = None

    # -> store output
    if write_local: # write to file on local machine
        # check if corresponding directory exists
        # if not, create
        scene_dir = data_root + 'scenes/' + dl_id_short
        
#         print(scene_dir)
        try: 
            os.makedirs(scene_dir)
        except OSError:
            if not os.path.isdir(scene_dir):
                raise
        # write cloud score map (and cloud mask? water mask?) to disk
        scorepath = scene_dir+'/'+dl_id_short+'_'+str(tile_res)+'m'+'_'+'p'+str(tile_pad)+'_'+\
            'tile'+str(tile_id).zfill(zfill)+'_'+'cloudscore'+'.tif'
        #print scorepath
        geo = tile['properties']['geotrans']
        prj = str(tile['properties']['wkt'])
        util_rasters.write_1band_geotiff(scorepath, cloud_scores, geo, prj, data_type=gdal.GDT_Float32)
        if store_cloudmask:
            cloudpath = scene_dir+'/'+dl_id_short+'_'+str(tile_res)+'m'+'_'+'p'+str(tile_pad)+'_'+\
            'tile'+str(tile_id).zfill(zfill)+'_'+'cloudmask'+'.tif'
            util_rasters.write_1band_geotiff(cloudpath, cloud_mask, geo, prj, data_type=gdal.GDT_Byte)
        if store_watermask and make_watermask:
            waterpath = scene_dir+'/'+dl_id_short+'_'+str(tile_res)+'m'+'_'+'p'+str(tile_pad)+'_'+\
                'tile'+str(tile_id).zfill(zfill)+'_'+'watermask'+'.tif'
            util_rasters.write_1band_geotiff(waterpath, water_mask, geo, prj, data_type=gdal.GDT_Byte)
        if store_predictions:
            predpath = scene_dir+'/'+dl_id_short+'_'+str(tile_res)+'m'+'_'+'p'+str(tile_pad)+'_'+\
                'tile'+str(tile_id).zfill(zfill)+'_'+'pred'+'.tif'
            util_rasters.write_multiband_geotiff(predpath, pred, geo, prj, data_type=gdal.GDT_Float32)
        lulcpath = scene_dir+'/'+dl_id_short+'_'+str(tile_res)+'m'+'_'+'p'+str(tile_pad)+'_'+\
            'tile'+str(tile_id).zfill(zfill)+'_'+'lulc'+'.tif'
        util_rasters.write_1band_geotiff(lulcpath, lulc, geo, prj, data_type=gdal.GDT_Byte)
    else: #write to dl catalog
        d=2

    return cloud_mask, cloud_scores, lulc, water_mask

def map_scenes_simple(scene_ids, tiles, network, zfill=None, store_predictions=True):
    if zfill is None:
        zfill = len(str(len(tiles['features'])-1))
    for scene_id in scene_ids:
        print(scene_id)
        # calculate scene extent
        for tile_id in range(len(tiles['features'])):
    #     for tile_id in range(2000,2001):
            fail_count=0
            try:
    #             print('tile #',tile_id)
                if tile_id % 1000 == 0:
                    print('tile #',tile_id)
                # test if tile intersects with scene; if not, skip
                map_tile(scene_id, tiles['features'][tile_id], tile_id, network, zfill=zfill, store_predictions=store_predictions)
            except Exception as e:
                print ('Error encountered mapping tile #', tile_id)
                print (e)
                fail_count = fail_count + 1
                if fail_count > 5:
                    break
                else:
                    tile_id = tile_id - 1
                    time.sleep(5)
                    continue
            fail_count = 0


def prep_lulc_derivation_arrays(lulc_paths, score_paths):
    assert len(lulc_paths)==len(score_paths)
    img, geo, prj, cols, rows = util_rasters.load_geotiff(lulc_paths[0],dtype='uint8')
    assert img.ndim==2
    lulcs = np.zeros((len(lulc_paths),)+img.shape, dtype='uint8')
    scores = np.zeros((len(lulc_paths),)+img.shape, dtype='float32')
    for i in range(len(lulc_paths)):
        lulc_img, lulc_geo, lulc_prj, lulc_cols, lulc_rows = util_rasters.load_geotiff(lulc_paths[i],dtype='uint8')
        assert img.shape==lulc_img.shape
        assert geo==lulc_geo
        assert prj==lulc_prj
        assert cols==lulc_cols
        assert rows==lulc_rows
        scores_img, scores_geo, scores_prj, scores_cols, scores_rows = util_rasters.load_geotiff(score_paths[0],dtype='float32')
        assert img.shape==scores_img.shape
        assert geo==scores_geo
        assert prj==scores_prj
        assert cols==scores_cols
        assert rows==scores_rows
        lulcs[i]=lulc_img
        scores[i]=scores_img
    return lulcs, scores

# lulcs and scores are already numpy stacks of 2d maps
def derive_lulc_map_binary(lulcs, scores, categories=[0,1,2], threshold=0.05):
    array_shape = lulcs[0].shape
    cats = list(categories)
    cats.append(255)
    votes = np.zeros(((len(cats),)+array_shape), dtype='uint8')
    valid_stack = (scores<=threshold)
    
    for i in range(len(cats)):
        c = cats[i]
        mask_stack = (lulcs==c)
        #print(mask_stack)
        votes_stack = mask_stack & valid_stack
        vote_count = np.sum(votes_stack, axis=0)
        #print(vote_count)
        votes[i] = vote_count
    cat_votes = np.sum(votes[:-1], axis=0)
    nodata_mask = (cat_votes==0)
#     print(votes)
    winner_indices = np.argmax(votes[:-1], axis=0)
    lulc_derived = np.zeros(array_shape, dtype='uint8')
    for i in range(len(cats)):
        mask = (winner_indices==i)
        lulc_derived[mask] = cats[i]
    lulc_derived[nodata_mask]=255
#     print(winner_indices)
#     print(lulc_derived)
    return lulc_derived

# lulcs and scores are already numpy stacks of 2d maps
def derive_lulc_map_weighted(lulcs, scores, categories=[0,1,2], threshold=0.5, stretch=False):
    array_shape = lulcs[0].shape
    cats = list(categories)
    cats.append(255)
    votes = np.zeros(((len(cats),)+array_shape), dtype='float32')
    valid_masks = (scores<=threshold)
    
    if stretch:
        reverse_scores = np.subtract(np.ones(scores.shape, dtype='float32'), np.divide(scores, threshold))
    else:
        reverse_scores = np.subtract(np.ones(scores.shape, dtype='float32'), scores) # 1 - scores
    
    for i in range(len(cats)):
        c = cats[i]
        cat_masks = (lulcs==c)
        full_masks = (cat_masks & valid_masks)
        votes_stack = np.multiply(full_masks, reverse_scores)
        votes[i] = np.sum(votes_stack, axis=0)
    
    cat_votes = np.sum(votes[:-1], axis=0)
    nodata_mask = (cat_votes==0)
#     print(votes)
    winner_indices = np.argmax(votes[:-1], axis=0)
    lulc_derived = np.zeros(array_shape, dtype='uint8')
    for i in range(len(cats)):
        mask = (winner_indices==i)
        lulc_derived[mask] = cats[i]
    lulc_derived[nodata_mask]=255
#     print(winner_indices)
#     print(lulc_derived)
    return lulc_derived