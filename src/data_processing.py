# all data processing related functions

import os, time, sys
import pandas as pd
import numpy as np
import xarray as xr
from scipy.stats import gamma, norm
from scipy.interpolate import interp1d

########################################################################################################################
# data transformation

def boxcox_transform(data, texp=4):
    # transform prcp to approximate normal distribution
    # mode: box-cox; power-law
    if not isinstance(data, np.ndarray):
        data = np.array(data)
    data = data.copy()
    data[data<0] = 0
    datat = (data ** (1 / texp) - 1) / (1 / texp)
    return datat

def boxcox_back_transform(data, texp=4):
    # transform prcp to approximate normal distribution
    # mode: box-cox; power-law
    if not isinstance(data, np.ndarray):
        data = np.array(data)
    data = data.copy()
    data[data<-texp] = -texp
    datat = (data / texp + 1) ** texp
    return datat

def boxcox_back_transform_biasadjustment(data, sigma_square, texp=4):
    # Box-Cox back transformation can lead to bias
    # This function is put here for future use
    # mode: box-cox; power-law
    # Reference: https://otexts.com/fpp2/transformations.html
    data = data.copy()
    if not isinstance(data, np.ndarray):
        data = np.array(data)
    data[data < -texp] = -texp
    datat = (data / texp + 1) ** texp * (1 + sigma_square * (1 - 1 / texp) / (2 * (data / texp + 1) ** 2))

    datat[data == -texp] = 0

    return datat

def interpolate_with_noise(cdfs, noise_level=1e-6):
    # Add small random noise to the 'Value' column
    for stn in cdfs.keys():
        for month in cdfs[stn].keys():
            cdf = cdfs[stn][month]
            cdf['Value'] += np.random.uniform(-noise_level, noise_level, cdf.shape[0])
            # Optional: Sort by 'Value' if needed
            cdf.sort_values(by='Value', inplace=True)
            cdfs[stn][month] = cdf

    return cdfs

def create_cdf_df(data):
    """Helper function to calculate the CDF for a given month."""
    valid_data = data[data > 0]
    sorted_data = np.sort(valid_data)
    cdf_values = np.arange(1, len(sorted_data) + 1) / len(sorted_data)
    return pd.DataFrame({'Value': sorted_data, 'CDF': cdf_values})

def add_nearby_stations_to_cdfs(cdfs, all_station_data_ds, nearby_station_ds,var='prcp', min_obs=10):
    """Add nearby stations to the CDFs if the number of observations is less than a set value."""

    measured_var = all_station_data_ds[var]
    # Get indexes for valid stations (at least one non-NaN values)
    stations_with_values = []
    for stn_index in range(measured_var.sizes['stn']):
        # Check if there are any non-NaN values in this station
        if np.any(~np.isnan(measured_var.sel(stn=stn_index))):
            stations_with_values.append(stn_index)

    nearby_stations = nearby_station_ds['nearIndex_InStn_prcp'].values

    for i in range(1,13):

        for stn_index in stations_with_values:
            #Check number of available observations
            num_obs = cdfs[stn_index][i]['Value'].size
            station_cdf = cdfs[stn_index][i]['Value'].values

            #Set initial empty arrays
            near_station_cdf_values = np.array([])
            station_cdf = np.array([])

            if num_obs < min_obs:
                iteration = 0
                
                while num_obs < min_obs and iteration < 5:
                    # Read nearby station cdf
                    near_station_cdf = cdfs[nearby_stations[stn_index][iteration]][i]

                    #Combine values
                    if iteration == 0:
                        station_cdf = cdfs[stn_index][i]['Value'].values
                    near_station_cdf_values = near_station_cdf['Value'].values
                    station_cdf = np.concatenate((station_cdf,near_station_cdf_values))
                    
                    #Update checks
                    num_obs = station_cdf.size
                    iteration += 1
                    
                cdfs[stn_index][i] = create_cdf_df(station_cdf)
    
    return cdfs

def calculate_monthly_cdfs(stn_ds, var_name, settings, nearby_stn_ds=None):
    """Create monthly empiricial CDFs."""

    pooled = settings.get('pooled', True)
    min_num_obs = settings.get('min_num_obs', 20)
    add_noise = settings.get('add_noise', True)

    df = pd.DataFrame(data=stn_ds[var_name].T.values, index=pd.to_datetime(stn_ds['time']))

    if pooled:
        cdfs = {month: create_cdf_df(df[df.index.month == month].values.flatten())
                for month in range(1, 13)}
    else:
        cdfs = {station: {month: create_cdf_df(df[station][df.index.month == month].dropna())
                         for month in range(1, 13)}
                for station in df.columns}
        
        if add_noise:
            cdfs = interpolate_with_noise(cdfs)

        if nearby_stn_ds is not None:
        # Check for minimum number of observations, and if not met, add nearby stations observations
            cdfs = add_nearby_stations_to_cdfs(cdfs, stn_ds, nearby_stn_ds, var=var_name, min_obs=min_num_obs)

    return cdfs

def normal_quantile_transform(data, times, monthly_cdfs, settings):
    """
    Apply a normal quantile transform to the precipitation data using a pooled monthly empirical cdf.
    """

    # Read settings and if not available, assign default value
    pooled = settings.get('pooled', True)
    min_z_value = settings.get('min_z_value', -4)

    df = pd.DataFrame(data=data.T, index=times)
    transformed_data = pd.DataFrame(index=df.index, columns=(df.columns if pooled else None))

    # Read all stations that only contain nan values
    nan_columns = [col for col in df.columns if df[col].isna().all()]

    for month in range(1, 13):
        for station in (df.columns if not pooled else [None]):    

            month_data = df[station][df.index.month == month] if not pooled else df[df.index.month == month]
            month_data = month_data[month_data > 0]
            
            empirical_cdf = monthly_cdfs.get(month) if pooled else monthly_cdfs[station][month]
            
            if empirical_cdf is not None and not empirical_cdf.empty:

                cdf_interp = interp1d(empirical_cdf['Value'], empirical_cdf['CDF'],bounds_error=False)
                cum_probs = np.clip(cdf_interp(month_data),0,0.99999)
                z_scores = norm.ppf(cum_probs)
            
                if pooled:
                    transformed_data.loc[month_data.index, :] = z_scores
                else:
                    transformed_data.loc[month_data.index, station] = z_scores
            else:
                if pooled:
                    transformed_data.loc[month_data.index, :] = np.nan
                else:
                    transformed_data.loc[month_data.index, station] = np.nan
            

        transformed_data_array = transformed_data.astype(float).to_numpy()

        #Assign min value to all nan
        transformed_data_filled = np.nan_to_num(transformed_data_array,nan=min_z_value)

        #Remove stations that had nan from start
        for col_index, col_name in enumerate(df.columns):
            if col_name in nan_columns:
                transformed_data_filled[:, col_index] = np.nan

    return transformed_data_filled

def inverse_normal_quantile_transform(data,time,monthly_cdfs, settings):
    """
    Reverse the normal quantile transform applied to the precipitation data using a monthly empirical cdf.
    """
    
    #Read settings value, and if not assign default value
    pooled = settings.get('pooled', True)
    interp_method = settings.get('interp_method', 'interp1d')
    min_est_value = settings.get('min_est_value', 0.01)

    if data.ndim == 3: #Grid regression
        flattened_data = data.reshape(-1, len(data[0,0,]))
        transformed_data = pd.DataFrame(flattened_data.T, index=time)
        back_transformed_data = pd.DataFrame(index=transformed_data.index, columns=transformed_data.columns)
    elif data.ndim == 2: #Station regression
        transformed_data = pd.DataFrame(data.T, index=time)
        back_transformed_data = pd.DataFrame(index=transformed_data.index, columns=transformed_data.columns)

    for month in range(1, 13):
        for station in (transformed_data.columns if not pooled else [None]):
            
            # Get z scores for each month, and for unpooled approach for each station
            z_scores = transformed_data[station][transformed_data.index.month == month] if not pooled else transformed_data[transformed_data.index.month == month]
            # Get precomputed ecdf values
            empirical_cdf = monthly_cdfs.get(month) if pooled else monthly_cdfs[station][month]

            if empirical_cdf is not None and not empirical_cdf.empty:

                if interp_method == 'interp1d':
                    # Use linear interpolation for the inverse transformation
                    value_interp = interp1d(empirical_cdf['CDF'], empirical_cdf['Value'], kind='linear', bounds_error=False, fill_value='extrapolate')
                    # Calculate cumulative probabilities from z scores
                    z_score_float = z_scores.values.astype(float)
                    cum_probs = norm.cdf(z_score_float)
                    # Use linear interpolation to sample from probabilities back to values
                    original_values = value_interp(cum_probs)

                #Filter small values created from filling of nan during first transform
                original_values[original_values < min_est_value] = np.nan
                #Convert all nan to zero
                original_values = np.nan_to_num(original_values)

                if pooled:
                    back_transformed_data.loc[z_scores.index, :] = original_values
                else:
                    back_transformed_data.loc[z_scores.index, station] = original_values
            else:
                if pooled:
                    back_transformed_data.loc[z_scores.index, :] = np.zeros(len(z_scores))
                else:
                    back_transformed_data.loc[z_scores.index, station] = np.zeros(len(z_scores))

    #Convert to array
    back_transform_array = back_transformed_data.astype(float).to_numpy()
    

    #Extra transform for grid regression
    if data.ndim == 3:
       back_transform_array = back_transform_array.T
       back_transform_array = back_transform_array.reshape(np.shape(data))

    return back_transform_array

def gamma_transform(data, times, settings, gamma_stations_ds):

    """
    Apply gamma transform based on pre-calculated mean and value values from empirical data
    """

    # Read settings and if not available, assign default value
    min_z_value = settings.get('min_z_value', -4)

    df = pd.DataFrame(data=data.T, index=times)
    transformed_data = pd.DataFrame(index=df.index, columns=df.columns)

    months = range(1, 13)
    stations = df.columns

    for month in months:
        for station in stations:    
            #Read data for specific month, and remove all zero and negative data
            month_data = df[station][df.index.month == month]
            month_data = month_data[month_data > 0]
            
            if np.all(np.isnan(month_data)):
                transformed_data.loc[month_data.index, station] = np.nan
            else:            
                #Read pre-computed gamma values
                a = gamma_stations_ds['gamma_shape'].sel(station=station,month=month).values.astype(float)
                scale = gamma_stations_ds['gamma_scale'].sel(station=station,month=month).values.astype(float)
                loc = np.zeros_like(scale)
                
                # Perform transformation
                cdf_interp = lambda x: gamma.cdf(x, a=a, loc=loc, scale=scale)
                cum_probs = cdf_interp(month_data)
                z_scores = norm.ppf(cum_probs)

                #Write to output array
                transformed_data.loc[month_data.index, station] = z_scores
                

    # Convert data array to float datatype
    transformed_data_array = transformed_data.astype(float).to_numpy()

    #Assign min value to all nan
    transformed_data_filled = np.nan_to_num(transformed_data_array,nan=min_z_value)

    return transformed_data_filled

def gamma_back_transform(data, time, settings, gamma_station_ds, gamma_gridded_ds=None):
    """
    Reverse the normal quantile transform applied to the precipitation data using a monthly empirical cdf.
    """
 
    #Read settings value, and if not assign default value
    min_est_value = settings.get('min_est_value', 0.01)

    if data.ndim == 2: #Station back-transform
        transformed_data = pd.DataFrame(data.T, index=time)
        back_transformed_data = pd.DataFrame(index=transformed_data.index, columns=transformed_data.columns)

    elif data.ndim == 3: #Grid back-transform
        flattened_data = data.reshape(-1, len(data[0,0,]))
        transformed_data = pd.DataFrame(flattened_data.T, index=time)
        back_transformed_data = pd.DataFrame(index=transformed_data.index, columns=transformed_data.columns)

    months = range(1, 13)
    stations = [transformed_data.columns if data.ndim == 2 else [None]]
    for month in months:
        for station in stations:

            if data.ndim == 2:
                z_scores = transformed_data[station][transformed_data.index.month == month] 
            else:
                z_scores = transformed_data[transformed_data.index.month == month]

            if z_scores.empty:
                if data.ndim == 2:
                    back_transformed_data.loc[z_scores.index, station] = np.zeros#(len(transformed_data.columns))
                else:
                    back_transformed_data.loc[z_scores.index, :] = np.zeros
            else:
                if data.ndim == 2:
                    gamma_shape = gamma_station_ds['gamma_shape'].sel(station=station,month=month).values.astype(float)
                    gamma_scale = gamma_station_ds['gamma_scale'].sel(station=station,month=month).values.astype(float)
                    # Calculate cumulative probabilities from z scores
                    z_score_float = z_scores.values.astype(float)
                    cum_probs = norm.cdf(z_score_float)
                    gamma_ppf = lambda x: gamma.ppf(x, a=gamma_shape, loc=0, scale=gamma_scale)
                    # Use gamma distribution to sample from probabilities back to values
                    original_values = gamma_ppf(cum_probs)
                if data.ndim == 3:
                    a = gamma_gridded_ds['gamma_shape'].sel(month=month).values.astype(float)
                    scale = gamma_gridded_ds['gamma_scale'].sel(month=month).values.astype(float)
                    loc = np.zeros_like(scale)
                    a_flat = a.flatten()
                    loc_flat = loc.flatten()
                    scale_flat = scale.flatten()
                    # Calculate cumulative probabilities from z scores
                    z_score_float = z_scores.values.astype(float)
                    cum_probs = norm.cdf(z_score_float)
                    cum_probs = np.clip(cum_probs, 0,0.99999)
                    original_values = np.zeros_like(cum_probs)
                    # Loop over each timestep
                    gamma_ppf = lambda x: gamma.ppf(x, a=a_flat, loc=loc_flat, scale=scale_flat)
                    for i in range(cum_probs.shape[0]):
                        original_values[i] = gamma_ppf(cum_probs[i])
                
                #Filter small values created from filling of nan during first transform
                original_values[original_values < min_est_value] = np.nan

                #Convert all nan to zero
                original_values = np.nan_to_num(original_values)

                if data.ndim == 3:
                    back_transformed_data.loc[z_scores.index, :] = original_values
                else:
                    back_transformed_data.loc[z_scores.index, station] = original_values


    #Convert to array
    back_transform_array = back_transformed_data.astype(float).to_numpy()
    
    #Extra transform for grid regression
    if data.ndim == 3:
        back_transform_array_interim = back_transform_array.T
        back_transform_array = back_transform_array_interim.reshape(np.shape(data))

    return back_transform_array

def data_transformation(data, method, settings, mode='transform',times=None,cdfs=None,gamma_stations_ds=None,gamma_gridded_ds=None):
    if method == 'boxcox':
        if mode == 'transform':
            data = boxcox_transform(data, settings['exponent'])
        elif mode == 'back_transform':
            data = boxcox_back_transform(data, settings['exponent'])
        else:
            print('Unknown transformation mode: entry=', mode); sys.exit()
    elif method == 'ecdf':
        if mode == 'transform':
            data = normal_quantile_transform(data,times,cdfs,settings)
            data = data.T
        elif mode == 'back_transform':
            data = inverse_normal_quantile_transform(data,times,cdfs,settings)
            if data.ndim == 2:
                data = data.T
        else:
            print('Unknown transformation mode: entry=', mode)
            sys.exit()
    elif method == 'gamma_monthly':
        if mode == 'transform':
            data = gamma_transform(data,times,settings,gamma_stations_ds)
            data = data.T
        elif mode == 'back_transform':
            data = gamma_back_transform(data,times,settings,gamma_stations_ds,gamma_gridded_ds)
            if data.ndim == 2:
                data = data.T
        else:
            print('Unknown transformation mode: entry=', mode)
            sys.exit()
    else:
        print('Unknown transformation method: entry=', method); sys.exit()
    return data


########################################################################################################################
# input station data processing

def merge_stndata_into_single_file(config):
    # GMET v2.0 assumes that each station has an independent file. GPEP will merge the station data into one file,
    #   which can speed up i/o in subsequent runs of GPEP using the same dataset. 
    t1 = time.time()

    # parse and change configurations
    outpath_parent = config['outpath_parent']
    path_stn_info  = f'{outpath_parent}/stn_info'
    file_allstn    = f'{path_stn_info}/all_station_data.nc'     # set default name for a merged station data file
    os.makedirs(path_stn_info, exist_ok=True)

    config['path_stn_info'] = path_stn_info
    config['file_allstn']   = file_allstn             # store default name for a merged station data file in config

    # in/out information to this function
    if 'input_stn_list' in config:
        input_stn_list = config['input_stn_list']
    else:
        input_stn_list = ''
    if 'input_stn_path' in config:
        input_stn_path = config['input_stn_path']
    else:
        input_stn_path = ''
    if 'input_stn_all' in config:
        input_stn_all  = config['input_stn_all']
    else:
        input_stn_all  = ''

    file_allstn = config['file_allstn']
    input_vars  = config['input_vars']
    target_vars = config['target_vars']

    if 'minRange_vars' in config:
        minRange_vars = config['minRange_vars']
        if isinstance(minRange_vars, (int, float)):
            minRange_vars = [minRange_vars] * len(target_vars)
    else:
        minRange_vars = [-np.inf] * len(target_vars)

    if 'maxRange_vars' in config:
        maxRange_vars = config['maxRange_vars']
        if isinstance(maxRange_vars, (int, float)):
            maxRange_vars = [maxRange_vars] * len(target_vars)
    else:
        maxRange_vars = [np.inf] * len(target_vars)

    if 'transform_vars' in config:
        transform_vars = config['transform_vars']
        if not isinstance(transform_vars, list):
            transform_vars = [transform_vars] * len(target_vars)
    else:
        transform_vars = [''] * len(target_vars)

    if 'transform' in config:
        transform_settings = config['transform']
    else:
        transform_settings = {}

    if 'mapping_InOut_var' in config:
        mapping_InOut_var  = config['mapping_InOut_var']
    else:
        mapping_InOut_var = []

        
    if 'overwrite_merged_stnfile' in config:
        overwrite_merged_stnfile = config['overwrite_merged_stnfile']
    else:
        overwrite_merged_stnfile = True

    # settings and prints
    print('#' * 50)
    print('Merging individual station files to one single file')
    print('#' * 50)
    print('Input station list:     ', input_stn_list)
    print('Input station folder:   ', input_stn_path)
    print('Output station file:    ', file_allstn)
    print('Target variables:       ', input_vars)

    if os.path.isfile(file_allstn):
        print('NOTE: Merged station file exists')
        if overwrite_merged_stnfile == True:
            print('overwrite_merged_stnfile is True. Continue.')
        else:
            print('overwrite_merged_stnfile is False. Skip station merging.\n')
            return config

    # load station data
    if 'input_stn_all' in config and os.path.isfile(input_stn_all):
        print('input_stn_all exists:    ', input_stn_all)
        print('reading station info from', input_stn_all, 'instead of individual files')
        #print('reading station information from', file_allstn, 'instead of individual files')  # this looks incorrect

        ds_stn = xr.load_dataset(input_stn_all)

        if os.path.isfile(input_stn_list):
            df_stn = pd.read_csv(input_stn_list)
            for col in df_stn.columns:
                ds_stn[col] = xr.DataArray(df_stn[col].values, dims=('stn'))

    else:
        print('A merged station data file does not exist: reading station information from individual files')

        df_stn = pd.read_csv(input_stn_list)
        nstn   = len(df_stn)

        infile0 = f'{input_stn_path}/{df_stn.stnid.values[0]}.nc'
        with xr.open_dataset(infile0) as ds:
            time_coord = ds.time
            ntime = len(ds.time)

        var_values = np.nan * np.zeros([nstn, ntime, len(input_vars)], dtype=np.float32)
        print(f'Number of input station: {nstn}')
        print(f'Time steps:              {ntime}')
        print('Start merging station data ... ')

        for i in range(nstn):
            infilei = f'{input_stn_path}/{df_stn.stnid.values[i]}.nc'
            dsi = xr.load_dataset(infilei)
            for j in range(len(input_vars)):
                if input_vars[j] in dsi.data_vars:
                    var_values[i, :, j] = np.squeeze(dsi[input_vars[j]].values)

        ds_stn                = xr.Dataset()
        ds_stn.coords['time'] = time_coord
        ds_stn.coords['stn']  = np.arange(nstn)

        for col in df_stn.columns:
            ds_stn[col] = xr.DataArray(df_stn[col].values, dims=('stn'))

        for i in range(len(input_vars)):
            ds_stn[input_vars[i]] = xr.DataArray(var_values[:, :, i], dims=('stn', 'time'))

        del var_values

    if 'gamma_monthly' in config['transform'].keys():
        config['gamma_station_nc'] = f"{config['path_stn_info']}/gamma_station_params.nc"
        config['gamma_gridded_nc'] = f"{config['path_stn_info']}/gamma_gridded_params.nc"


    # convert input vars to target vars
    for mapping in mapping_InOut_var:
        mapping = mapping.replace(' ', '')
        tarvar  = mapping.split('=')[0]

        if tarvar in target_vars:

            if tarvar in ds_stn:
                print(f'{tarvar} is already in ds_stn. no need to perform {mapping}')
            else:
                invars = [v for v in input_vars if v in mapping]
                for v in invars:
                    exec(f"{v}=ds_stn.{v}")

                operation      = mapping.split('=')[1]
                ds_stn[tarvar] = eval(operation)

    # constrain variables
    for i in range(len(target_vars)):
        vari = target_vars[i]
        if vari in ds_stn.data_vars:
            v = ds_stn[vari].values
            if np.any(v < minRange_vars[i]):
                print(f'{vari} station data has values < {minRange_vars[i]}. Adjust those to {minRange_vars[i]}.')
                v[v < minRange_vars[i]] = minRange_vars[i]
            if np.any(v > maxRange_vars[i]):
                print(f'{vari} station data has values > {maxRange_vars[i]}. Adjust those to {maxRange_vars[i]}.')
                v[v > maxRange_vars[i]] = maxRange_vars[i]
            ds_stn[vari].values = v

    # transform variables
    print('Transform variables if relevant settings are provided')
    for i in range(len(transform_vars)):
        if len(transform_vars[i])>0:
            tvar = target_vars[i] + '_' + transform_vars[i]
            print(f'Perform {transform_vars[i]} transformation for {target_vars[i]}. Add a new variable {tvar} to output station file.')
            if tvar in ds_stn:
                print(f'{tvar} exists in ds_stn. no need to perform transformation')
                continue
            ds_stn[tvar] = ds_stn[vari].copy()

            if transform_vars[i] == 'ecdf':
                cdfs = calculate_monthly_cdfs(ds_stn,target_vars[i],transform_settings[transform_vars[i]])
                ds_stn[tvar].values = data_transformation(ds_stn[target_vars[i]].values, transform_vars[i],
                                                transform_settings[transform_vars[i]], 'transform',
                                                times=ds_stn['time'].values,cdfs=cdfs)
            elif transform_vars[i] == 'gamma_monthly':
                ds_stn[tvar].values = data_transformation(ds_stn[target_vars[i]].values, transform_vars[i],
                                                transform_settings[transform_vars[i]], 'transform',
                                                times=ds_stn['time'].values, gamma_stations_ds=xr.open_dataset(config['gamma_station_nc']))
            else:
                ds_stn[tvar].values = data_transformation(ds_stn[target_vars[i]].values, transform_vars[i],
                                                transform_settings[transform_vars[i]], 'transform')
        else:
            print(f'Do not perform transformation for {target_vars[i]}')

    # save to output files
    encoding = {}
    for var in ds_stn.data_vars:
        encoding[var] = {'zlib': True, 'complevel': 4}

    if 'stnid' in ds_stn:
        try:
            ds_stn['stnid'] = ds_stn['stnid'].astype('|S')
        except:
            print('Failed to process station ID information.')

    ds_stn.to_netcdf(file_allstn, encoding=encoding)

    t2 = time.time()
    print('Time cost (s) for merging station data file:', t2-t1, '\n')

    return config


