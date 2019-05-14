import logging
from urllib.error import URLError


import pandas as pd
from pvlib import iotools
from requests.exceptions import HTTPError


from solarforecastarbiter.io.reference_observations import common


solrad_variables = ['ghi', 'dni', 'dhi']

SOLRAD_FTP_DIR = "ftp://aftp.cmdl.noaa.gov/data/radiation/solrad/"
REALTIME_URL = SOLRAD_FTP_DIR + "/realtime/{abbr}/{abrv}{year_2d}{jday}.dat"
ARCHIVE_URL = SOLRAD_FTP_DIR + "/{abrv}/{year}/{abrv}{year_2d}{jday}.dat"

logger = logging.getLogger('reference_data')


def fetch(api, site, start, end, realtime=False):
    """Retrieve observation data for a solrad site between start and end.

    Parameters
    ----------
    api : io.APISession
        An APISession with a valid JWT for accessing the Reference Data
        user.
    site : datamodel.Site
        Site object with the appropriate metadata.
    start : datetime
        The beginning of the period to request data for.
    end : datetime
        The end of the period to request data for.
    realtime : bool
        Whether or not to look for realtime data. Note that this data is
        raw, unverified data from the instruments.

    Returns
    -------
    data : pandas.DataFrame
        All of the requested data concatenated into a single DataFrame.
    """
    if realtime:
        url_format = REALTIME_URL
    else:
        url_format = ARCHIVE_URL
    # load extra parameters for api arguments.
    extra_params = common.decode_extra_parameters(site)
    abbreviation = extra_params['network_api_abbreviation']
    single_day_dfs = []
    for day in pd.date_range(start, end):
        filename = url_format.format(abrv=abbreviation,
                                     year=day.year,
                                     year_2d=day.strftime('%y'),
                                     jday=day.strftime('%j'))
        logger.info(f'Requesting data for SOLRAD site {site.name}'
                    f' on {day.strftime("%Y%m%d")}.')
        try:
            # Only get dataframe from the returned tuple
            solrad_day = iotools.read_solrad(filename)
        except URLError:
            logger.warning(f'Could not retrieve SOLRAD data for site '
                           f'{site.name} on {day.strftime("%Y%m%d")}.')
            logger.debug(f'Failed SOLRAD URL: {filename}.')
            continue
        else:
            single_day_dfs.append(solrad_day)
    all_period_data = pd.concat(single_day_dfs)
    return all_period_data


def initialize_site_observations(api, site):
    """Creates an observaiton at the site for each variable in solrad_variables.

    Parameters
    ----------
    site : datamodel.Site
        The site object for which to create Observations.
    """
    for variable in solrad_variables:
        try:
            common.create_observation(api, site, variable)
        except HTTPError as e:
            logger.error(f'Failed to create {variable} observation at Site '
                         f'{site.name}. Error: {e}')


def update_observation_data(api, sites, observations, start, end):
    """Post new observation data to all reference observations
    at each SOLRAD site between start and end.

    api : solarforecastarbiter.io.api.APISession
        An active Reference user session.
    sites : list of solarforecastarbiter.datamodel.Site
        List of all reference sites as Objects
    observations : list of solarforecastarbiter.datamodel.Observation
        List of all reference observations.
    start : datetime
        The beginning of the period to request data for.
    end : datetime
        The end of the period to request data for.
    """
    solrad_sites = common.filter_by_networks(sites, 'NOAA SOLRAD')
    for site in solrad_sites:
        common.update_noaa_site_observations(
            api, fetch, site, observations, start, end)
