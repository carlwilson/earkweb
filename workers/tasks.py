import time
import os
import tarfile
import traceback
import shutil
from os import walk
import logging
from functools import partial
import glob
import sys

from celery import Task
from celery import current_task
from earkcore.metadata.mets.MetsValidation import MetsValidation
from earkcore.metadata.mets.ParsedMets import ParsedMets

from earkcore.packaging.extraction import Extraction
from sandbox.sipgenerator.sipgenerator import SIPGenerator
from config import params
from config.config import root_dir
from earkcore.utils import fileutils
from earkcore.models import InformationPackage
from earkcore.utils import randomutils
from earkcore.xml.deliveryvalidation import DeliveryValidation
from taskresult import TaskResult
from workers.default_task import DefaultTask
from workers.statusvalidation import StatusValidation
from earkcore.metadata.mets.MetsManipulate import Mets
from earkcore.fixity.ChecksumAlgorithm import ChecksumAlgorithm
from earkcore.metadata.premis.PremisManipulate import Premis
from earkcore.metadata.identification import MetaIdentification
from earkcore.utils.fileutils import increment_file_name_suffix
from earkcore.utils.fileutils import latest_aip
from tasklogger import TaskLogger
from earkcore.rest.restendpoint import RestEndpoint
from earkcore.rest.hdfsrestclient import HDFSRestClient
from search.models import DIP
from earkcore.filesystem.chunked import FileBinaryDataChunks
from earkcore.fixity.ChecksumFile import ChecksumFile
from earkcore.fixity.tasklib import check_transfer
from earkcore.utils.fileutils import mkdir_p
from workers.ip_state import IpState
from earkcore.packaging.task_utils import get_deliveries
from earkcore.utils.fileutils import remove_fs_item


def custom_progress_reporter(task, percent):
    task.update_state(state='PROGRESS', meta={'process_percent': percent})

def init_task(pk_id, task_name, task_logfile_name):
    start_time = time.time()
    ip = InformationPackage.objects.get(pk=pk_id)
    if not ip.uuid:
        ip.uuid = randomutils.getUniqueID()
    ip_work_dir = os.path.join(params.config_path_work, ip.uuid)
    task_log_file_dir = os.path.join(ip_work_dir, 'metadata')
    task_log_file = os.path.join(task_log_file_dir, "%s.log" % task_logfile_name)
    # create working directory
    if not os.path.exists(ip_work_dir):
        os.mkdir(ip_work_dir)
    # create log directory
    if not os.path.exists(task_log_file_dir):
        os.mkdir(task_log_file_dir)
    # create PREMIS file or return handle to task
    if os.path.isfile(task_log_file_dir + '/PREMIS.xml'):
        with open(task_log_file_dir + '/PREMIS.xml', 'rw') as premis_file:
            package_premis_file = Premis(premis_file)
    elif not os.path.isfile(task_log_file_dir + '/PREMIS.xml'):
        premis_skeleton_file = root_dir + '/earkresources/PREMIS_skeleton.xml'
        with open(premis_skeleton_file, 'r') as premis_file:
            package_premis_file = Premis(premis_file)
        package_premis_file.add_agent('eark-aip-creation')
    tl = TaskLogger(task_log_file)
    tl.addinfo(("%s task %s" % (task_name, current_task.request.id)))
    return ip, ip_work_dir, tl, start_time, package_premis_file

def init_task2(ip_work_dir, task_name, task_logfile_name):
    start_time = time.time()
    # create working directory
    if not os.path.exists(ip_work_dir):
        os.mkdir(ip_work_dir)
    metadata_dir = os.path.join(ip_work_dir, 'metadata')
    task_log_file = os.path.join(metadata_dir, "%s.log" % task_logfile_name)
    # create log directory
    if not os.path.exists(metadata_dir):
        os.mkdir(metadata_dir)
    # create PREMIS file or return handle to task
    if os.path.isfile(metadata_dir + '/PREMIS.xml'):
        with open(metadata_dir + '/PREMIS.xml', 'rw') as premis_file:
            package_premis_file = Premis(premis_file)
    elif not os.path.isfile(metadata_dir + '/PREMIS.xml'):
        premis_skeleton_file = root_dir + '/earkresources/PREMIS_skeleton.xml'
        with open(premis_skeleton_file, 'r') as premis_file:
            package_premis_file = Premis(premis_file)
        package_premis_file.add_agent('eark-aip-creation')
    tl = TaskLogger(task_log_file)
    tl.addinfo(("%s task %s" % (task_name, current_task.request.id)))
    return tl, start_time, package_premis_file

def handle_error(ip, tc, tl):
    ip.statusprocess = tc.error_status
    ip.save()
    tb = traceback.format_exc()
    tl.adderr(("An error occurred: %s" % str(tb)))
    logging.error(tb)
    return tl.fin()


def add_PREMIS_event(task, outcome, identifier_value,  linking_agent, package_premis_file,
                     tl, ip_work_dir):
    '''
    Add an event to the PREMIS file and update it afterwards.
    '''
    package_premis_file.add_event(task, outcome, identifier_value, linking_agent)
    path_premis = os.path.join(ip_work_dir, "metadata/PREMIS.xml")
    with open(path_premis, 'w') as output_file:
        output_file.write(package_premis_file.to_string())
    tl.addinfo('PREMIS file updated: %s' % path_premis)


class SIPCreationReset(DefaultTask):

    accept_input_from = ['All']

    def run_task(self, task_context):
        """
        SIP Creation Reset run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:14,type:4
        """
        # implementation
        task_context.task_status = 0
        return {}


class SIPPackageMetadataCreation(DefaultTask):

    accept_input_from = ['SIPCreationReset', 'SIPPackageMetadataCreation']

    def run_task(self, task_context):
        """
        SIP Package metadata creation run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:13,type:4
        """
        os.chdir(task_context.path)
        task_context.task_logger.addinfo("Working in rootdir %s" % os.getcwd())
        sipgen = SIPGenerator()
        sipgen.createMets()

        task_context.task_status = 0
        return {}

class SIPPackaging(DefaultTask):

    accept_input_from = [SIPPackageMetadataCreation.__name__, 'SIPPackaging']

    def run_task(self, task_context):
        """
        SIP Packaging run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:14,type:4
        """
        task_context.task_logger.addinfo("Package name: %s" % task_context.additional_input['packagename'])

        # reload(sys)
        # sys.setdefaultencoding('utf8')
        #
        # # append generation number to tar file; if tar file exists, the generation number is incremented
        # storage_tar_file = task_context.
        # tar = tarfile.open(storage_tar_file, "w:")
        # tl.addinfo("Packaging working directory: %s" % ip_work_dir)
        # total = sum([len(files) for (root, dirs, files) in walk(ip_work_dir)])
        # tl.addinfo("Total number of files in working directory %d" % total)
        # # log file is closed at this point because it will be included in the package,
        # # subsequent log messages can only be shown in the gui
        # tl.fin()
        # i = 0
        # for subdir, dirs, files in os.walk(ip_work_dir):
        #     for file in files:
        #         entry = os.path.join(subdir, file)
        #         tar.add(entry)
        #         if i % 10 == 0:
        #             perc = (i * 100) / total
        #             self.update_state(state='PROGRESS', meta={'process_percent': perc})
        #         i += 1
        # tar.close()
        # tl.log.append("Package stored: %s" % storage_tar_file)


        task_context.task_status = 0
        return {}


class SIPtoAIPReset(DefaultTask):

    accept_input_from = ['All']

    def run_task(self, task_context):
        """
        SIP to AIP Reset run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:1,type:1
        """
        # create working directory if it does not exist
        if not os.path.exists(task_context.path):
            fileutils.mkdir_p(task_context.path)

        # remove and recreate empty directories
        items_to_remove = ['METS.xml', 'data', 'metadata', 'Content', 'Metadata']
        for item in items_to_remove:
            remove_fs_item(task_context.uuid, task_context.path, item)

        # remove extracted sips
        deliveries = get_deliveries(task_context.path, task_context.task_logger)
        for delivery in deliveries:
            if os.path.exists(str(delivery)):
                shutil.rmtree(str(delivery))

        # success status
        task_context.task_status = 0
        return {'identifier': ""}


class SIPDeliveryValidation(DefaultTask):

    accept_input_from = [SIPtoAIPReset.__name__]

    def run_task(self, task_context):
        """
        SIP Delivery Validation run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:2,type:1
        """
        tl = task_context.task_logger
        deliveries = get_deliveries(task_context.path, task_context.task_logger)
        if len(deliveries) == 0:
            tl.adderr("No delivery found in working directory")
            task_context.task_status = 1
        else:
            for delivery in deliveries:
                tar_file = deliveries[delivery]['tar_file']
                delivery_file = deliveries[delivery]['delivery_xml']
                tl.addinfo("Package file: %s" % delivery_file)
                tl.addinfo("Delivery XML file: %s" % delivery_file)
                schema_file = os.path.join(task_context.path, 'IP_CS_mets.xsd')
                tl.addinfo("Schema file: %s" % schema_file)
                sdv = DeliveryValidation()
                validation_result = sdv.validate_delivery(task_context.path, delivery_file, schema_file, tar_file)
                tl.log = tl.log + validation_result.log
                tl.err = tl.err + validation_result.err
                tl.addinfo("Delivery validation result (xml/file size/checksum): %s" % validation_result.valid)
                if not validation_result.valid:
                    tl.adderr("Delivery invalid: %s" % delivery)
                    task_context.task_status = 1
                else:
                    task_context.task_status = 0
        return


class IdentifierAssignment(DefaultTask):

    accept_input_from = [SIPDeliveryValidation.__name__]

    def run_task(self, task_context):
        """
        Identifier Assignment run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:3,type:1
        """
        # TODO: set identifier in METS file
        identifier = randomutils.getUniqueID()
        task_context.task_logger.addinfo("New identifier assigned: %s" % identifier)
        task_context.task_status = 0
        return {'identifier': identifier}


class SIPExtraction(DefaultTask):

    accept_input_from = [IdentifierAssignment.__name__]

    def run_task(self, task_context):
        """
        SIP Extraction run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:4,type:1
        """
        tl = task_context.task_logger
        deliveries = get_deliveries(task_context.path, task_context.task_logger)
        if len(deliveries) == 0:
            tl.adderr("No delivery found in working directory")
            task_context.task_status = 1
        else:
            extr = Extraction()
            for delivery in deliveries:
                tar_file = deliveries[delivery]['tar_file']
                custom_reporter = partial(custom_progress_reporter, self)
                extr.extract_with_report(tar_file, task_context.path, progress_reporter=custom_reporter)
            tl.log += extr.log
            tl.err += extr.err
        task_context.task_status = 0
        return


class SIPRestructuring(DefaultTask):

    accept_input_from = [SIPExtraction.__name__]

    def run_task(self, task_context):
        """
        SIP Restructuring run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:5,type:1
        """
        tl = task_context.task_logger
        deliveries = get_deliveries(task_context.path, task_context.task_logger)
        if len(deliveries) == 0:
            tl.adderr("No delivery found in working directory")
            task_context.task_status = 1
        else:
            for delivery in deliveries:
                tl.addinfo("Restructuring content of package: %s" % str(delivery))

                fs_childs =  os.listdir(str(delivery))
                for fs_child in fs_childs:
                    source_item = os.path.join(str(delivery), fs_child)
                    target_folder = task_context.path
                    tl.addinfo("Move SIP folder '%s' to '%s" % (source_item, target_folder))
                    shutil.move(source_item, target_folder)
                os.removedirs(str(delivery))

            task_context.task_status = 0
        return


class SIPValidation(DefaultTask):

    accept_input_from = [SIPRestructuring.__name__]

    def run_task(self, task_context):
        """
        SIP Validation run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:6,type:1
        """
        tl = task_context.task_logger
        path = task_context.path
        def check_file(descr, f):
            if os.path.exists(f):
                tl.addinfo("%s found: %s" % (descr, os.path.abspath(f)))
            else:
                tl.adderr(("%s missing: %s" % (descr, os.path.abspath(f))))
        check_file("SIP METS file", os.path.join(path, "METS.xml"))
        check_file("Data directory", os.path.join(path, "data"))
        check_file("Content directory", os.path.join(path, "data/content"))
        check_file("Documentation directory", os.path.join(path, "data/documentation"))
        check_file("Metadata directory", os.path.join(path, "metadata"))
        mets_file = os.path.join(path, "METS.xml")
        parsed_mets = ParsedMets(os.path.join(path))
        parsed_mets.load_mets(mets_file)
        mval = MetsValidation(parsed_mets)
        size_val_result = mval.validate_files_size()
        tl.log += size_val_result.log
        tl.err += size_val_result.err
        valid = (len(tl.err) == 0)
        task_context.task_status = 0 if valid else 1
        return


class AIPCreation(DefaultTask):

    accept_input_from = [SIPValidation.__name__]

    def run_task(self, task_context):
        """
        AIP Creation run task
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:7,type:1
        """
        tl = task_context.task_logger
        # #package_dir = os.path.join(ip_work_dir, ip.packagename)
        # submission_dir = os.path.join(task_context.path, "submission")
        # package_in_submission_dir = os.path.join(submission_dir, ip.packagename)
        # shutil.move(task_context.path, package_in_submission_dir)
        # tl.addinfo("Package directory %s moved to submission directory %s" % (package_dir, package_in_submission_dir))
        #
        # # create submission mets
        # mets_skeleton_file = root_dir + '/earkresources/METS_skeleton.xml'
        # with open(mets_skeleton_file, 'r') as mets_file:
        #     submission_mets_file = Mets(wd=ip_work_dir, alg=ChecksumAlgorithm.SHA256)
        # # my_mets.add_dmd_sec('EAD', 'file://./metadata/EAD.xml')
        # admids = []
        # admids.append(submission_mets_file.add_tech_md('file://./metadata/PREMIS.xml#Obj'))
        # admids.append(submission_mets_file.add_digiprov_md('file://./metadata/PREMIS.xml#Ingest'))
        # admids.append(submission_mets_file.add_rights_md('file://./metadata/PREMIS.xml#Right'))
        # submission_mets_file.add_file_grp(['submission'])
        # submission_mets_file.add_file_grp(['schemas'])
        # # add a file group for metadata files that are not classified
        # submission_mets_file.add_file_grp(['customMD'])
        # # TODO: rel_path_mets has to be changed according to how the METS file is named
        # rel_path_mets = "file://./submission/%s/%s" % (ip.packagename, "METS.xml")
        #
        # submission_mets_file.add_file(['submission'], rel_path_mets, admids)
        # # TODO: set header with list of attributes
        # # retrieve METS root tag attributes
        # mets_attributes = params.mets_attributes
        # for item in mets_attributes.items():
        #     submission_mets_file.root.set(item[0], item[1])
        #
        # # path length
        # workdir_length = len(ip_work_dir)
        #
        # # cover uppercase/lowercase in sub directories
        # directory_list =  os.listdir(package_in_submission_dir)
        # content_directory = ''
        # metadata_directory = ''
        # for subdir in directory_list:
        #     if os.path.isdir(package_in_submission_dir + '/' + subdir):
        #         if subdir.lower() == 'content':
        #             content_directory = '/' + subdir
        #         elif subdir.lower() == 'metadata':
        #             metadata_directory = '/' + subdir
        #
        # # retrieve files in /Content
        # for directory, subdirectories, filenames in os.walk(package_in_submission_dir + content_directory):
        #     for filename in filenames:
        #         rel_path_file = 'file://.' + directory[workdir_length:] + '/' + filename
        #         # create METS entry:
        #         submission_mets_file.add_file(['submission'], rel_path_file, admids)
        #         # create PREMIS object entry:
        #         package_premis_file.add_object(rel_path_file)
        #
        # # retrieve files in /Metadata
        # md_type_list = ['ead', 'eac', 'premis', 'mets']
        # for directory, subdirectories, filenames in os.walk(package_in_submission_dir + metadata_directory):
        #     for filename in filenames:
        #         rel_path_file = 'file://.' + directory[workdir_length:] + '/' + filename
        #         # create PREMIS object entry:
        #         package_premis_file.add_object(rel_path_file)
        #         # TODO: add to metadata sections? tech_md, rights_md, digiprov_md?
        #         # TODO: different filegrp for schemas?
        #         # create METS entry:
        #         if filename[-3:] == 'xsd':
        #             submission_mets_file.add_file(['schemas'], rel_path_file, admids)
        #         elif filename[-3:] == 'xml':
        #             if (filename[:3].lower() == 'ead' or filename[-7:].lower() == 'ead.xml'):
        #                 submission_mets_file.add_dmd_sec('ead', rel_path_file)
        #             elif (filename[:3].lower() == 'eac' or filename[-7:].lower() == 'eac.xml'):
        #                 submission_mets_file.add_dmd_sec('eac', rel_path_file)
        #             elif (filename[:6].lower() == 'premis' or filename[-10:].lower() == 'premis.xml'):
        #                 submission_mets_file.add_tech_md(rel_path_file, '')
        #             elif filename:
        #                 xml_tag = MetaIdentification.MetaIdentification(directory + '/' + filename)
        #                 if xml_tag.lower() in md_type_list:
        #                 # TODO see rules above, and add accordingly
        #                     if xml_tag.lower() == 'eac' or xml_tag.lower() == 'ead':
        #                         submission_mets_file.add_dmd_sec(xml_tag.lower(), rel_path_file)
        #                     elif xml_tag:
        #                         submission_mets_file.add_tech_md(rel_path_file, '')
        #                 elif xml_tag.lower() not in md_type_list:
        #                 # custom metadata format?
        #                     submission_mets_file.add_file(['customMD'], rel_path_file, admids)
        #                     # print 'found a custom xml file: ' + filename + ' with tag: ' + xml_tag
        #
        # submission_mets_file.add_file(['submission'], rel_path_mets, admids)
        # submission_mets_file.root.set('TYPE', 'AIP')
        # submission_mets_file.root.set('ID', ip.uuid)
        #
        # path_mets = os.path.join(submission_dir, "METS.xml")
        # with open(path_mets, 'w') as output_file:
        #     output_file.write(submission_mets_file.to_string())
        # tl.addinfo(("Submission METS file created: %s" % rel_path_mets))
        # valid = xx
        # task_context.task_status = 0 if valid else 1
        tl.addinfo("Not implemented yet.")
        task_context.task_status = 0
        return


class AIPCreation(Task, StatusValidation):

    accept_input_from = [SIPValidation.__name__]

    def run_task(self, task_context):
        """
        SIP Validation
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:7,type:1
        """
        ip, ip_work_dir, tl, start_time, package_premis_file = init_task(pk_id, "AIPCreation", "sip_to_aip_processing")
        tl.err = self.valid_state(ip, tc)

        if len(tl.err) > 0:
            return tl.fin()
        try:


            valid = True
            ip.statusprocess = tc.success_status if valid else tc.error_status
            ip.save()
            self.update_state(state='PROGRESS', meta={'process_percent': 100})

            # update the PREMIS file at the end of the task - SUCCESS
            add_PREMIS_event('AIPCreation', 'SUCCESS', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return tl.fin()
        except Exception, e:
            # update the PREMIS file at the end of the task - FAILURE
            tl.error("ERROR:"+str(e))
            add_PREMIS_event('AIPCreation', 'FAILURE', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return handle_error(ip, tc, tl)


class AIPValidation(Task, StatusValidation):
    def run(self, pk_id, tc, *args, **kwargs):
        """
        AIP Validation
        @type       pk_id: int
        @param      pk_id: Primary key
        @type       tc: TaskConfig
        @param      tc: order:8,type:1
        @rtype:     TaskResult
        @return:    Task result (success/failure, processing log, error log)
        """
        ip, ip_work_dir, tl, start_time, package_premis_file = init_task(pk_id, "AIPValidation", "sip_to_aip_processing")
        tl.err = self.valid_state(ip, tc)
        if len(tl.err) > 0:
            return tl.fin()

        try:
            tl.addinfo("AIP always validates, this task is not implemented yet")
            valid = True # TODO: Implement AIP validation
            ip.statusprocess = tc.success_status if valid else tc.error_status
            ip.save()
            self.update_state(state='PROGRESS', meta={'process_percent': 100})

            # update the PREMIS file at the end of the task - SUCCESS
            add_PREMIS_event('AIPValidation', 'SUCCESS', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return tl.fin()
        except Exception, err:
            # update the PREMIS file at the end of the task - FAILURE
            add_PREMIS_event('AIPValidation', 'FAILURE', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return handle_error(ip, tc, tl)


class AIPPackaging(Task, StatusValidation):
    def run(self, pk_id, tc, *args, **kwargs):
        """
        AIP Packaging
        @type       pk_id: int
        @param      pk_id: Primary key
        @type       tc: TaskConfig
        @param      tc: order:9,type:1
        @rtype:     TaskResult
        @return:    Task result (success/failure, processing log, error log)
        """
        ip, ip_work_dir, tl, start_time, package_premis_file = init_task(pk_id, "AIPPackaging", "sip_to_aip_processing")
        tl.err = self.valid_state(ip, tc)
        if len(tl.err) > 0:
            return tl.fin()
        try:
            # identifier (not uuid of the working directory) is used as first part of the tar file
            ip_storage_dir = os.path.join(params.config_path_storage, ip.identifier)
            import sys

            reload(sys)
            sys.setdefaultencoding('utf8')
            # append generation number to tar file; if tar file exists, the generation number is incremented
            storage_tar_file = increment_file_name_suffix(ip_storage_dir, "tar")
            tar = tarfile.open(storage_tar_file, "w:")
            tl.addinfo("Packaging working directory: %s" % ip_work_dir)
            total = sum([len(files) for (root, dirs, files) in walk(ip_work_dir)])
            tl.addinfo("Total number of files in working directory %d" % total)
            # log file is closed at this point because it will be included in the package,
            # subsequent log messages can only be shown in the gui
            tl.fin()
            i = 0
            for subdir, dirs, files in os.walk(ip_work_dir):
                for file in files:
                    entry = os.path.join(subdir, file)
                    tar.add(entry)
                    if i % 10 == 0:
                        perc = (i * 100) / total
                        self.update_state(state='PROGRESS', meta={'process_percent': perc})
                    i += 1
            tar.close()
            tl.log.append("Package stored: %s" % storage_tar_file)
            result = tl.fin()
            ip.statusprocess = tc.success_status if result.success else tc.error_status
            ip.save()
            self.update_state(state='PROGRESS', meta={'process_percent': 100})

            # update the PREMIS file at the end of the task - SUCCESS
            add_PREMIS_event('AIPPackaging', 'SUCCESS', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return result
        except Exception, err:
            # update the PREMIS file at the end of the task - FAILURE
            add_PREMIS_event('AIPPAckaging', 'FAILURE', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return handle_error(ip, tc, tl)


class LilyHDFSUpload(Task, StatusValidation):
    def run(self, pk_id, tc, *args, **kwargs):
        """
        Lily HDFS Upload
        @type       pk_id: int
        @param      pk_id: Primary key
        @type       tc: TaskConfig
        @param      tc: order:10,type:1
        @rtype:     TaskResult
        @return:    Task result (success/failure, processing log, error log)
        """
        ip, ip_work_dir, tl, start_time, package_premis_file = init_task(pk_id, "LilyHDFSUpload", None)
        tl.err = self.valid_state(ip, tc)
        if len(tl.err) > 0:
            return tl.fin()
        try:
            # identifier (not uuid of the working directory) is used as first part of the tar file
            ip_storage_dir = os.path.join(params.config_path_storage, ip.identifier)
            aip_path = latest_aip(ip_storage_dir, 'tar')

            tl.addinfo("Start uploading AIP %s from local path: %s" % (ip.identifier, aip_path))

            if aip_path is not None:

                # Reporter function which will be passed via the HDFSRestClient to the FileBinaryDataChunks.chunks()
                # method where the actual reporting about the upload progress occurs.

                rest_endpoint = RestEndpoint("http://81.189.135.189", "dm-hdfs-storage")
                tl.addinfo("Using REST endpoint: %s" % (rest_endpoint.to_string()))

                # Partial application of the custom_progress_reporter function so that the task object
                # is known to the FileBinaryDataChunks.chunks() method.
                partial_custom_progress_reporter = partial(custom_progress_reporter, self)
                hdfs_rest_client = HDFSRestClient(rest_endpoint, partial_custom_progress_reporter)
                rest_resource_path = "hsink/fileresource/files/{0}"

                upload_result = hdfs_rest_client.upload_to_hdfs(aip_path, rest_resource_path)
                tl.addinfo("Upload finished in %d seconds with status code %d: %s" % (time.time() - start_time, upload_result.status_code, upload_result.hdfs_path_id))

                checksum_resource_uri = "hsink/fileresource/files/%s/digest/sha-256" % upload_result.hdfs_path_id
                tl.addinfo("Verifying checksum at %s" % (checksum_resource_uri))
                hdfs_sha256_checksum = hdfs_rest_client.get_string(checksum_resource_uri)

                if ChecksumFile(aip_path).get(ChecksumAlgorithm.SHA256) == hdfs_sha256_checksum:
                    tl.addinfo("Checksum verification completed, the package was transmitted successfully.")
                else:
                    tl.adderr("Checksum verification failed, an error occurred while trying to transmit the package.")

                result = tl.fin()
                ip.statusprocess = tc.success_status if result.success else tc.error_status
                ip.save()

                # update the PREMIS file at the end of the task - SUCCESS
                add_PREMIS_event('LilyHDFSUpload', 'SUCCESS', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
                return result
            else:
                tl.adderr("No AIP file found for identifier: %s" % ip.identifier)
                add_PREMIS_event('LilyHDFSUpload', 'FAILURE', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
                return tl.fin()
        except Exception:
            # update the PREMIS file at the end of the task - FAILURE
            add_PREMIS_event('LilyHDFSUpload', 'FAILURE', 'identifier', 'agent', package_premis_file, tl, ip_work_dir)
            return handle_error(ip, tc, tl)


class AIPtoDIPReset(DefaultTask):

    accept_input_from = ['All']

    def run_task(self, task_context):
        """
        SIP Validation
        @type       tc: task configuration line (used to insert read task properties in database table)
        @param      tc: order:11,type:2
        """
        # create working directory if it does not exist
        if not os.path.exists(task_context.path):
            fileutils.mkdir_p(task_context.path)

        # remove and recreate empty directories
        # data_path = os.path.join(task_context.path, "data")
        # if os.path.exists(data_path):
        #     shutil.rmtree(data_path)
        # mkdir_p(data_path)
        # task_context.task_logger.addinfo("New empty 'data' directory created")
        # metadata_path = os.path.join(task_context.path, "metadata")
        # if os.path.exists(metadata_path):
        #     shutil.rmtree(metadata_path)
        # mkdir_p(metadata_path)
        # task_context.task_logger.addinfo("New empty 'metadata' directory created")
        # # remove extracted sips
        # tar_files = glob.glob("%s/*.tar" % task_context.path)
        # for tar_file in tar_files:
        #     tar_base_name, _ = os.path.splitext(tar_file)
        #     if os.path.exists(tar_base_name):
        #         shutil.rmtree(tar_base_name)
        #     task_context.task_logger.addinfo("Extracted SIP folder '%s' removed" % tar_base_name)

        # success status
        task_context.task_status = 0
        return {} #{'identifier': ""}


class DIPAcquireAIPs(Task, StatusValidation):
    def run(self, pk_id, tc, *args, **kwargs):
        """
        DIP Acquire AIPs
        @type       pk_id: int
        @param      pk_id: Primary key
        @type       tc: TaskConfig
        @param      tc: order:12,type:2
        @rtype:     TaskResult
        @return:    Task result (success/failure, processing log, error log)
        """
        ip, ip_work_dir, tl, start_time, package_premisfile = init_task(pk_id, "DIPAcquireAIPs", "aip_to_dip_processing")
        tl.err = self.valid_state(ip, tc)
        if len(tl.err) > 0:
            return tl.fin()
        try:
            # create dip working directory
            if not os.path.exists(ip_work_dir):
                os.mkdir(ip_work_dir)

            # packagename is identifier of the DIP creation process
            dip = DIP.objects.get(name=ip.packagename)

            total_bytes_read = 0
            if dip.all_aips_available():
                dip_aips_total_size = dip.aips_total_size()
                tl.addinfo("DIP: %s, total size: %d" % (ip.packagename, dip_aips_total_size))
                for aip in dip.aips.all():

                    partial_custom_progress_reporter = partial(custom_progress_reporter, self)
                    package_extension = aip.source.rpartition('.')[2]
                    aip_in_dip_work_dir = os.path.join(ip_work_dir, ("%s.%s" % (aip.identifier, package_extension)))
                    tl.addinfo("Source: %s (%d)" % (aip.source, aip.source_size()))
                    tl.addinfo("Target: %s" % (aip_in_dip_work_dir))
                    with open(aip_in_dip_work_dir, 'wb') as target_file:
                        for chunk in FileBinaryDataChunks(aip.source, 65536, partial_custom_progress_reporter).chunks(total_bytes_read, dip_aips_total_size):
                            target_file.write(chunk)
                        if len(tl.err) > 0:
                            return tl.fin()
                        total_bytes_read += aip.source_size()
                        target_file.close()
                    check_transfer(aip.source, aip_in_dip_work_dir, tl)
                self.update_state(state='PROGRESS', meta={'process_percent': 100})
            result = tl.fin()
            ip.statusprocess = tc.success_status if result.success else tc.error_status
            ip.save()
            return result
        except Exception:
            return handle_error(ip, tc, tl)



class DIPExtractAIPs(Task, StatusValidation):
    def run(self, pk_id, tc, *args, **kwargs):
        """
        DIP Extract AIPs
        @type       pk_id: int
        @param      pk_id: Primary key
        @type       tc: TaskConfig
        @param      tc: order:13,type:2
        @rtype:     TaskResult
        @return:    Task result (success/failure, processing log, error log)
        """
        ip, ip_work_dir, tl, start_time, package_premisfile = init_task(pk_id, "DIPExtractAIPs", "aip_to_dip_processing")
        tl.err = self.valid_state(ip, tc)
        if len(tl.err) > 0:
            return tl.fin()
        try:
            # create dip working directory
            if not os.path.exists(ip_work_dir):
                os.mkdir(ip_work_dir)

            # packagename is identifier of the DIP creation process
            dip = DIP.objects.get(name=ip.packagename)

            def get_tar_object(aip):
                import sys
                reload(sys)
                sys.setdefaultencoding('utf8')
                package_extension = aip.source.rpartition('.')[2]
                aip_in_dip_work_dir = os.path.join(ip_work_dir, ("%s.%s" % (aip.identifier, package_extension)))
                return tarfile.open(name=aip_in_dip_work_dir, mode='r', encoding='utf-8')

            if dip.all_aips_available():
                total_members = 0
                for aip in dip.aips.all():
                    tar_obj = get_tar_object(aip)
                    members = tar_obj.getmembers()
                    total_members += len(members)
                    tar_obj.close()

                tl.addinfo("DIP: %s, total number of entries: %d" % (ip.packagename, total_members))
                total_processed_members = 0
                perc = 0
                for aip in dip.aips.all():
                    package_extension = aip.source.rpartition('.')[2]
                    aip_in_dip_work_dir = os.path.join(ip_work_dir, ("%s.%s" % (aip.identifier, package_extension)))
                    tl.addinfo("Extracting: %s" % aip_in_dip_work_dir)
                    tar_obj = tarfile.open(name=aip_in_dip_work_dir, mode='r', encoding='utf-8')
                    members = tar_obj.getmembers()
                    current_package_total_members = 0
                    for member in members:
                        if total_processed_members % 10 == 0:
                            perc = (total_processed_members * 100) / total_members
                            self.update_state(state='PROGRESS', meta={'process_percent': perc})
                        tar_obj.extract(member, ip_work_dir)
                        tl.addinfo(("File extracted: %s" % member.name), display=False)
                        total_processed_members += 1
                        current_package_total_members += 1
                    ip.statusprocess = tc.success_status
                    ip.save()
                    tl.addinfo("Extraction of %d items from package %s finished" % (current_package_total_members, aip.identifier))
                tl.addinfo(("Extraction of %d items in total finished" % total_processed_members))
                self.update_state(state='PROGRESS', meta={'process_percent': 100})
            else:
                tl.addinfo("All AIPs must be accessible to perform this task")
            result = tl.fin()
            ip.statusprocess = tc.success_status if result.success else tc.error_status
            ip.save()
            return result
        except Exception:
            return handle_error(ip, tc, tl)

# def finalize(tl, ted):
#     task_doc_path = os.path.join(ted.get_path(), "task.xml")
#     task_doc_task_id_path = os.path.join(ted.get_path(), "task-%s.xml"  % current_task.request.id)
#     ted.write_doc(task_doc_path)
#     ted.write_doc(task_doc_task_id_path)
#     return tl.fin()


from earkweb.celeryapp import app

@app.task(bind=True)
def extract_and_remove_package(self, package_file_path, target_directory, proc_logfile):
    tl = TaskLogger(proc_logfile)
    extr = Extraction()
    proc_res = extr.extract(package_file_path, target_directory)
    if proc_res.success:
        tl.addinfo("Package %s extracted to %s" % (package_file_path, target_directory))
    else:
        tl.adderr("An error occurred while trying to extract package %s extracted to %s" % (package_file_path, target_directory))
    # delete file after extraction
    os.remove(package_file_path)
    return proc_res.success
