""" Analysis methods for TACA """
import csv
import glob
import logging
import os
import re
import subprocess
import taca.utils.undetermined as ud

from datetime import datetime

import requests

from taca.illumina import Run
from taca.utils.filesystem import chdir, control_fastq_filename
from taca.utils.config import CONFIG
from taca.utils import misc
from taca.illumina.flowcell_parser.classes import XTenSampleSheetParser

logger = logging.getLogger(__name__)


def is_transferred(run, transfer_file):
    """ Checks wether a run has been transferred to the analysis server or not.
        Returns true in the case in which the tranfer is ongoing.

    :param str run: Run directory
    :param str transfer_file: Path to file with information about transferred runs
    """
    try:
        with open(transfer_file, 'r') as file_handle:
            t_f = csv.reader(file_handle, delimiter='\t')
            for row in t_f:
                #Rows have two columns: run and transfer date
                if row[0] == os.path.basename(run):
                    return True
        if os.path.exists(os.path.join(run, 'transferring')):
            return True
        return False
    except IOError:
        return False


def transfer_run(run, analysis=True):
    """ Transfer a run to the analysis server. Will add group R/W permissions to
    the run directory in the destination server so that the run can be processed
    by any user/account in that group (i.e a functional account...). Run will be
    moved to data_dir/nosync after transferred.

    :param str run: Run directory
    :param bool analysis: Trigger analysis on remote server
    """
    with chdir(run):
        command_line = ['rsync', '-av']
        # Add R/W permissions to the group
        command_line.append('--chmod=g+rw')
        # rsync works in a really funny way, if you don't understand this, refer to
        # this note: http://silentorbit.com/notes/2013/08/rsync-by-extension/
        command_line.append("--include=*/")
        for to_include in CONFIG['analysis']['analysis_server']['sync']['include']:
            command_line.append("--include={}".format(to_include))
        command_line.extend(["--exclude=*", "--prune-empty-dirs"])
        r_user = CONFIG['analysis']['analysis_server']['user']
        r_host = CONFIG['analysis']['analysis_server']['host']
        r_dir = CONFIG['analysis']['analysis_server']['sync']['data_archive']
        remote = "{}@{}:{}".format(r_user, r_host, r_dir)
        command_line.extend([run, remote])

        # Create temp file indicating that the run is being transferred
        open('transferring', 'w').close()
        started = ("Started transfer of run {} on {}"
                   .format(os.path.basename(run), datetime.now()))
        logger.info(started)
        # In this particular case we want to capture the exception because we want
        # to delete the transfer file
        try:
            misc.call_external_command(command_line, with_log_files=True)
        except subprocess.CalledProcessError as exception:
            os.remove('transferring')
            raise exception

        t_file = os.path.join(CONFIG['analysis']['status_dir'], 'transfer.tsv')
        logger.info('Adding run {} to {}'
                    .format(os.path.basename(run), t_file))
        with open(t_file, 'a') as tranfer_file:
            tsv_writer = csv.writer(tranfer_file, delimiter='\t')
            tsv_writer.writerow([os.path.basename(run), str(datetime.now())])
        os.remove('transferring')

        if analysis:
            trigger_analysis(run)


def trigger_analysis(run_id):
    """ Trigger the analysis of the flowcell in the analysis sever.

    :param str run_id: run/flowcell id
    """
    if not CONFIG.get('analysis', {}).get('analysis_server', {}):
        logger.warn(("No configuration found for remote analysis server. "
                     "Not triggering analysis of {}"
                     .format(os.path.basename(run_id))))
    else:
        url = ("http://{host}:{port}/flowcell_analysis/{dir}"
               .format(host=CONFIG['analysis']['analysis_server']['host'],
                       port=CONFIG['analysis']['analysis_server']['port'],
                       dir=os.path.basename(run_id)))
        params = {'path': CONFIG['analysis']['analysis_server']['data_archive']}
        try:
            r = requests.get(url, params=params)
            if r.status_code != requests.status_codes.codes.OK:
                logger.warn(("Something went wrong when triggering the "
                             "analysis of {}. Please check the logfile "
                             "and make sure to start the analysis!"
                             .format(os.path.basename(run_id))))
            else:
                logger.info('Analysis of flowcell {} triggered in {}'
                            .format(os.path.basename(run_id),
                                    CONFIG['analysis']['analysis_server']['host']))
                a_file = os.path.join(CONFIG['analysis']['status_dir'], 'analysis.tsv')
                with open(a_file, 'a') as analysis_file:
                    tsv_writer = csv.writer(analysis_file, delimiter='\t')
                    tsv_writer.writerow([os.path.basename(run_id), str(datetime.now())])
        except requests.exceptions.ConnectionError:
            logger.warn(("Something went wrong when triggering the analysis "
                         "of {}. Please check the logfile and make sure to "
                         "start the analysis!".format(os.path.basename(run_id))))


def prepare_sample_sheet(run):
    """ This is a temporary function in order to solve the current problem with LIMS system
        not able to generate a compatible samplesheet for HiSeqX. This function needs to massage
        the sample sheet created by GenoLogics in order to correctly demultiplex HiSeqX runs.
        This function returns with success if the samplesheet is in the correct place, otherwise
        this flowcell will not be processed.

        :param str run: Run directory
    """
    #start by checking if samplesheet is in the correct place
    run_name = os.path.basename(run)
    current_year = '20' + run_name[0:2]
    samplesheets_dir = os.path.join(CONFIG['analysis']['samplesheets_dir'],
                                    current_year)

    run_name_componets = run_name.split("_")
    FCID = run_name_componets[3][1:]

    FCID_samplesheet_origin = os.path.join(samplesheets_dir, FCID + '.csv')
    FCID_samplesheet_dest   = os.path.join(run, "SampleSheet.csv")

    ss_reader=XTenSampleSheetParser(FCID_samplesheet_origin)
    #check that the samplesheet is not already present
    if os.path.exists(FCID_samplesheet_dest):
        logger.warn(("When trying to generate SampleSheet.csv for sample "
                     "sheet {}  looks like that SampleSheet.csv was already "
                     "present in {} !!".format(FCID, FCID_samplesheet_dest)))
        return False
    try:
        with open(FCID_samplesheet_dest, 'wb') as fcd:
            fcd.write(ss_reader.generate_clean_samplesheet(fields_to_remove=['index2'], rename_samples=True))
    except Exception as e:
        logger.error(e.text)
        return False


    # everything ended corretly
    return True





def run_preprocessing(run):
    """Run demultiplexing in all data directories

    :param str run: Process a particular run instead of looking for runs
    """

    def _process(run):
        """Process a run/flowcell and transfer to analysis server

        :param taca.illumina.Run run: Run to be processed and transferred
        """
        logger.info('Checking run {}'.format(run.id))
        if run.is_finished():
            if  run.status == 'TO_START':
                logger.info(("Starting BCL to FASTQ conversion and "
                             "demultiplexing for run {}".format(run.id)))
                # work around LIMS problem
                if prepare_sample_sheet(run.run_dir):
                    run.demultiplex()
            elif run.status == 'IN_PROGRESS':
                logger.info(("BCL conversion and demultiplexing process in "
                             "progress for run {}, skipping it"
                             .format(run.id)))
                ud.check_undetermined_status(run.run_dir, status=run.status, und_tresh=CONFIG['analysis']['undetermined']['lane_treshold'],
                   q30_tresh=CONFIG['analysis']['undetermined']['q30_treshold'], freq_tresh=CONFIG['analysis']['undetermined']['highest_freq'])
            elif run.status == 'COMPLETED':
                logger.info(("Preprocessing of run {} is finished, check if "
                             "run has been transferred and transfer it "
                             "otherwise".format(run.id)))

                control_fastq_filename(os.join(run.run_dir, CONFIG['analysis']['bcl2fastq']['options']['output_dir']))
                ud.check_undetermined_status(run.run_dir, status=run.status, und_tresh=CONFIG['analysis']['undetermined']['lane_treshold'],
                   q30_tresh=CONFIG['analysis']['undetermined']['q30_treshold'], freq_tresh=CONFIG['analysis']['undetermined']['highest_freq'])
                t_file = os.path.join(CONFIG['analysis']['status_dir'], 'transfer.tsv')
                transferred = is_transferred(run.run_dir, t_file)
                #####TESTING THINGY
                transferred=True
                ######
                if not transferred:
                    logger.info("Run {} hasn't been transferred yet."
                                .format(run.id))
                    logger.info('Transferring run {} to {} into {}'
                                .format(run.id,
                        CONFIG['analysis']['analysis_server']['host'],
                        CONFIG['analysis']['analysis_server']['sync']['data_archive']))
                    transfer_run(run.run_dir)
                else:
                    logger.info('Run {} already transferred to analysis server, skipping it'.format(run.id))

        if not run.is_finished():
            # Check status files and say i.e Run in second read, maybe something
            # even more specific like cycle or something
            logger.info('Run {} is not finished yet'.format(run.id))

    if run:
        _process(Run(run))
    else:
        data_dirs = CONFIG.get('analysis').get('data_dirs')
        for data_dir in data_dirs:
            runs = glob.glob(os.path.join(data_dir, '1*XX'))
            for _run in runs:
                _process(Run(_run))
