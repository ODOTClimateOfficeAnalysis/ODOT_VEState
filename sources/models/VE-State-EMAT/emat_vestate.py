
import os
import numpy as np
import pandas as pd
import logging
import tempfile
import re
import shutil
import platform
import subprocess
import json
from distutils.dir_util import copy_tree, remove_tree
import re, glob

from emat import Scope, SQLiteDB
from emat.model.core_files import FilesCoreModel
from emat.model.core_files.parsers import TableParser, MappingParser, loc, key

_logger = logging.getLogger("EMAT.VEState")

# The demo model code is located in the same
# directory as this script file.  We can recover
# this directory name like this, even if the
# current working directory is different.
this_directory = os.path.dirname(__file__)

def scenario_input(*filename):
	"""The path to a scenario_input file."""
	return os.path.join(this_directory, 'scenario_inputs', *filename)

def join_norm(*args):
	"""Normalize joined paths."""
	return os.path.normpath(os.path.join(*args))

def r_join_norm(*args):
	"""Normalize joined paths."""
	return os.path.normpath(os.path.join(*args)).replace('\\','/')

class VEStateModel(FilesCoreModel):
	"""
	A class for using the Vision Eval State Model as a files core model.

	Args:
		db (emat.Database, optional):
			An optional Database to store experiments and results.
			This allows this module to store results in a persistent
			manner across sessions.  If a `db` is not given, one is
			created and initialized in the temporary directory
			alongside the other model files, but it will be
			deleted automatically when the Python session ends.
		db_filename (str, default "vestate.db")
			The filename used to create a database if no existing
			database is given in `db`.
		scope (emat.Scope, optional):
			A YAML file that defines the scope for these model
			runs. If not given, the default scope stored in this
			package directly is used.
	"""

	def __init__(self, db=None, db_filename="vestate.db", scope=None):

		# Make a temporary directory for this instance.
		self.master_directory = tempfile.TemporaryDirectory(dir=join_norm(this_directory,'Temporary'))
		os.chdir(self.master_directory.name)
		_logger.warning(f"changing cwd to {self.master_directory.name}")
		cwd = self.master_directory.name

		# Housekeeping for this example:
		# Also copy the CONFIG and SCOPE files
		for i in ['model-config', 'scope']:
			shutil.copy2(
				join_norm(this_directory, 'vestate-emat-files', f"vestate-{i}.yml"),
				join_norm(cwd, f"vestate-{i}.yml"),
			)

		if scope is None:
			scope = Scope(join_norm(cwd, "vestate-scope.yml"))

		# Initialize a new daatabase if none was given.
		if db is None:
			if os.path.exists(db_filename):
				initialize = False
			else:
				initialize = True
			db = SQLiteDB(
				db_filename,
				initialize=initialize,
			)
		if db is False: # explicitly use no DB
			db = None
		else:
			if scope.name not in db.read_scope_names():
				db.store_scope(scope)

		# Initialize the super class (FilesCoreModel)
		super().__init__(
			configuration=join_norm(cwd, "vestate-model-config.yml"),
			scope=scope,
			db=db,
			name='VEState',
			local_directory = cwd,
		)
		if isinstance(db, SQLiteDB):
			self._sqlitedb_path = db.database_path

		# Populate the model_path directory of the files-based model.
		copy_tree(
			join_norm(this_directory, 'VE-State'),
			join_norm(cwd, self.model_path),
		)

		self._hist_datastore_dir = join_norm(self.config['model_hist_datastore'])

		# Ensure that R libraries can be found.
		r_lib = self.config['r_library_path']
		with open(join_norm(cwd, self.model_path, '.Rprofile'), 'wt') as rprof:
			rprof.write(f'.libPaths("{r_lib}")\n')

		# Add parsers to instruct the load_measures function
		# how to parse the outputs and get the measure values.

		# # ComputedMeasures.json
		# instructions = {}
		# for measure in scope.get_measures():
		# 	if measure.parser and measure.parser.get('file') == 'ComputedMeasures.json':
		# 		instructions[measure.name] = key[measure.parser.get('key')]
		# self.add_parser(
		# 	MappingParser(
		# 		"ComputedMeasures.json",
		# 		instructions,
		# 	)
		# )

		# CalcOregonValidationMeasures.R
		instructions = {}
		for measure in scope.get_measures():
			if measure.parser and measure.parser.get('file') == 'state_validation_measures.csv':
				if measure.parser.get('loc'):
					instructions[measure.name] = loc[(str(j) for j in measure.parser.get('loc'))]
				elif measure.parser.get('eval'):
					instructions[measure.name] = eval(measure.parser.get('eval'))
		self.add_parser(
			TableParser(
				"state_validation_measures.csv",
				instructions,
				index_col=0,
			)
		)


	def setup(self, params: dict):
		"""
		Configure the core model with the experiment variable values.

		This method is the place where the core model set up takes place,
		including creating or modifying files as necessary to prepare
		for a VEState core model run.  When running experiments, this method
		is called once for each core model experiment, where each experiment
		is defined by a set of particular values for both the exogenous
		uncertainties and the policy levers.  These values are passed to
		the experiment only here, and not in the `run` method itself.
		This facilitates debugging, as the `setup` method can potentially
		be used without the `run` method, allowing the user to manually
		inspect the prepared files and ensure they are correct before
		actually running a potentially expensive model.

		At the end of the `setup` method, a core model experiment should be
		ready to run using the `run` method.

		Args:
			params (dict):
				experiment variables including both exogenous
				uncertainty and policy levers

		Raises:
			KeyError:
				if a defined experiment variable is not supported
				by the core model
		"""
		_logger.info("VEState SETUP...")

		for p in self.scope.get_parameters():
			if p.name not in params:
				_logger.warning(f" - for {p.name} using default value {p.default}")
				params[p.name] = p.default

		super().setup(params)

		# Check if we are using distributed multi-processing. If so,
		# we'll need to copy some files into a local working directory,
		# as otherwise changes in the files will over-write each other
		# when different processes are working in a common directory at
		# the same time.
		try:
			# First try to import the dask.distributed library
			# and check if this code is running on a worker.
			from dask.distributed import get_worker
			worker = get_worker()
		except (ValueError, ImportError):
			# If the library is not available, or if the code is
			# not running on a worker, then we are not running
			# in multi-processing mode, and we can just use
			# the main cwd as the working directory without
			# copying anything.
			pass
		else:
			# If we do find we are running this setup on a
			# worker, then we want to set the local directory
			# accordingly. We copy model files from the "master"
			# working directory to the worker's local directory,
			# if it is different (it should be). Depending
			# on how large your core model is, you may or may
			# not want to be copying the whole thing.
			if self.local_directory != worker.local_directory:

				# Make the archive path absolute, so all archives
				# go back to the original directory.
				self.archive_path = os.path.abspath(self.resolved_archive_path)

				_logger.debug(f"DISTRIBUTED.COPY FROM {self.local_directory}")
				_logger.debug(f"                   TO {worker.local_directory}")
				copy_tree(
					join_norm(self.local_directory, self.model_path),
					join_norm(worker.local_directory, self.model_path),
				)
				self.local_directory = worker.local_directory

		# The process of manipulating each input file is broken out
		# into discrete sub-methods, as each step is loosely independent
		# and having separate methods makes this clearer.
		#self._manipulate_model_parameters_json(params)
		self._manipulate_income(params)
		self._manipulate_bikes(params)
		self._manipulate_fees(params)
		# self._manipulate_comsvc_vehage(params)
		#self._manipulate_land_use(params)
		#self._manipulate_transit(params)
		#self._manipulate_fuel_cost(params)
		#self._manipulate_technology_mix(params)
		#self._manipulate_parking(params)
		#self._manipulate_demand(params)
		#self._manipulate_vehicle_characteristics(params)
		#self._manipulate_driving_efficiency(params)
		#self._manipulate_vehicle_travel_cost(params)
		_logger.info("VEState SETUP complete")



	def _manipulate_model_parameters_json(self, params):
		"""
		Prepare the model_parameters input file based on the existing file.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""

		# load the text of the first demo input file
		with open(join_norm(self.local_directory, self.model_path, 'defs', 'model_parameters.json'), 'rt') as f:
			y = json.load(f)

		y[0]['VALUE'] = str(params['ValueOfTime'])

		# write the manipulated text back out to the first demo input file
		with open(join_norm(self.local_directory, self.model_path, 'defs', 'model_parameters.json'), 'wt') as f:
			json.dump(y, f)

	def _manipulate_income(self, params):
		"""
		Prepare the income input file based on a template file.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""

		income_df = pd.read_csv(join_norm(scenario_input('I','azone_per_cap_inc.csv')))
		
		income_df.loc[income_df.Year == 2025,['HHIncomePC.2005', 'GQIncomePC.2005']] = \
			income_df.loc[income_df.Year == 2025,['HHIncomePC.2005', 'GQIncomePC.2005']] * params['Income2025']
		income_df.loc[income_df.Year == 2030,['HHIncomePC.2005', 'GQIncomePC.2005']] = \
			income_df.loc[income_df.Year == 2030,['HHIncomePC.2005', 'GQIncomePC.2005']] * params['Income2030']
		income_df.loc[income_df.Year == 2035,['HHIncomePC.2005', 'GQIncomePC.2005']] = \
			income_df.loc[income_df.Year == 2035,['HHIncomePC.2005', 'GQIncomePC.2005']] * params['Income2035']
		income_df.loc[income_df.Year == 2040,['HHIncomePC.2005', 'GQIncomePC.2005']] = \
			income_df.loc[income_df.Year == 2040,['HHIncomePC.2005', 'GQIncomePC.2005']] * params['Income2040']
		income_df.loc[income_df.Year == 2045,['HHIncomePC.2005', 'GQIncomePC.2005']] = \
			income_df.loc[income_df.Year == 2045,['HHIncomePC.2005', 'GQIncomePC.2005']] * params['Income2045']
		income_df.loc[income_df.Year == 2050,['HHIncomePC.2005', 'GQIncomePC.2005']] = \
			income_df.loc[income_df.Year == 2050,['HHIncomePC.2005', 'GQIncomePC.2005']] * params['Income2050']

		out_filename = join_norm(
			self.resolved_model_path, 'inputs', 'azone_per_cap_inc.csv'
		)
		_logger.debug(f"writing updates to: {out_filename}")
		# with open(out_filename, 'wt') as f:
		# 	f.write(y)
		income_df.to_csv(out_filename, index=False)

	def _manipulate_comsvc_vehage(self, params):
		"""
		Prepare the income input file based on a template file.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""

		comsvc_vehage_df = pd.read_csv(join_norm(scenario_input('V','region_comsvc_veh_mean_age.csv')))
		
		comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2025,['AveComSvcVehicleAge']] = \
			comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2025,['AveComSvcVehicleAge']] * params['AveComSvcVehicleAge2025']
		comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2030,['AveComSvcVehicleAge']] = \
			comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2030,['AveComSvcVehicleAge']] * params['AveComSvcVehicleAge2030']
		comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2035,['AveComSvcVehicleAge']] = \
			comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2035,['AveComSvcVehicleAge']] * params['AveComSvcVehicleAge2035']
		comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2040,['AveComSvcVehicleAge']] = \
			comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2040,['AveComSvcVehicleAge']] * params['AveComSvcVehicleAge2040']
		comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2045,['AveComSvcVehicleAge']] = \
			comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2045,['AveComSvcVehicleAge']] * params['AveComSvcVehicleAge2045']
		comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2050,['AveComSvcVehicleAge']] = \
			comsvc_vehage_df.loc[comsvc_vehage_df.Year == 2050,['AveComSvcVehicleAge']] * params['AveComSvcVehicleAge2050']

		out_filename = join_norm(
			self.resolved_model_path, 'inputs', 'region_comsvc_veh_mean_age.csv'
		)
		_logger.debug(f"writing updates to: {out_filename}")
		# with open(out_filename, 'wt') as f:
		# 	f.write(y)
		comsvc_vehage_df.to_csv(out_filename, index=False)

	def _manipulate_bikes(self, params):
		"""
		Prepare the biking input file based on a template file.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""

		# computed_params = {}
		# computed_params['BikeDiversion'] = params['Bicycles']

		# with open(scenario_input('B','azone_prop_sov_dvmt_diverted.csv.template'), 'rt') as f:
		# 	y = f.read()

		# for n in computed_params.keys():
		# 	y = y.replace(
		# 		f"__EMAT_PROVIDES_{n}__",  # the token to replace
		# 		f"{computed_params[n]:.3f}"  # the value to replace it with (as a string)
		# 	)

		# out_filename = join_norm(
		# 	self.resolved_model_path, 'inputs', 'azone_prop_sov_dvmt_diverted.csv'
		# )
		# _logger.debug(f"writing updates to: {out_filename}")
		# with open(out_filename, 'wt') as f:
		# 	f.write(y)
		return self._manipulate_by_mixture(params, 'Bicycles', 'B',)

	def _manipulate_by_categorical_drop_in(self, params, cat_param, cat_mapping, ve_scenario_dir):
		"""
		Copy in the relevant input files.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""
		scenario_dir = cat_mapping.get(params[cat_param])
		for i in os.scandir(scenario_input(ve_scenario_dir,scenario_dir)):
			if i.is_file():
				shutil.copyfile(
					scenario_input(ve_scenario_dir,scenario_dir,i.name),
					join_norm(self.resolved_model_path, 'inputs', i.name)
				)

	def _manipulate_land_use(self, params):
		"""
		Copy in the relevant the land use input files.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""
		cat_mapping = {
			'base': '1',
			'growth': '2',
		}
		return self._manipulate_by_categorical_drop_in(params, 'LandUse', cat_mapping, 'L')

	def _manipulate_vehicle_travel_cost(self, params):
		cat_mapping = {
			'base': '1',
			'steady ownership cost': '2',
			'pay-per-mile insurance and higher cost': '3',
		}
		return self._manipulate_by_categorical_drop_in(params, 'VehicleTravelCost', cat_mapping, 'C')

	def _manipulate_transit(self, params):
		"""
		Prepare the income input file based on a template file.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""

		computed_params = {}
		computed_params['DRRevMi'] = params['Transit'] * 2381994.664
		computed_params['MBRevMi'] = params['Transit'] * 3580237.203

		with open(scenario_input('T','marea_transit_service.csv.template'), 'rt') as f:
			y = f.read()

		for n in computed_params.keys():
			y = y.replace(
				f"__EMAT_PROVIDES_{n}__",  # the token to replace
				f"{computed_params[n]:.3f}",  # the value to replace it with (as a string)
			)

		out_filename = join_norm(
			self.resolved_model_path, 'inputs', 'marea_transit_service.csv'
		)
		_logger.debug(f"writing updates to: {out_filename}")
		with open(out_filename, 'wt') as f:
			f.write(y)

	def _manipulate_fuel_cost(self, params):
		"""
		Prepare the fuel and electric input file based on a template file.

		Args:
			params (dict):
				The parameters for this experiment, including both
				exogenous uncertainties and policy levers.
		"""

		computed_params = {}
		computed_params['FuelCost'] = params['FuelCost']
		computed_params['ElectricCost'] = params['ElectricCost']

		with open(scenario_input('G','azone_fuel_power_cost.csv.template'), 'rt') as f:
			y = f.read()

		for n in computed_params.keys():
			y = y.replace(
				f"__EMAT_PROVIDES_{n}__",  # the token to replace
				f"{computed_params[n]:.3f}",  # the value to replace it with (as a string)
			)

		out_filename = join_norm(
			self.resolved_model_path, 'inputs', 'azone_fuel_power_cost.csv'
		)
		_logger.debug(f"writing updates to: {out_filename}")
		with open(out_filename, 'wt') as f:
			f.write(y)

	def _manipulate_technology_mix(self, params, ):
		return self._manipulate_by_mixture(params, 'TechMix', 'F',)

	def _manipulate_parking(self, params, ):
		return self._manipulate_by_mixture(params, 'Parking', 'P',)

	def _manipulate_demand(self, params, ):
		return self._manipulate_by_mixture(params, 'DemandManagement', 'D',)

	def _manipulate_vehicle_characteristics(self, params, ):
		return self._manipulate_by_mixture(params, 'VehicleCharacteristics', 'V',)

	def _manipulate_driving_efficiency(self, params, ):
		return self._manipulate_by_mixture(params, 'DrivingEfficiency', 'E',)

	def _manipulate_fees(self, params, ):
		# return self._manipulate_by_mixture(params, 'VMTFees', 'F',)
		cat_mapping = {
			'ap': '1',
			'sts': '2',
		}
		return self._manipulate_by_categorical_drop_in(params, 'VMTFees', cat_mapping, 'F')

	def _manipulate_by_mixture(self, params, weight_param, ve_scenario_dir, no_mix_cols=('Year', 'Geo',)):

		weight_2 = params[weight_param]
		weight_1 = 1.0-weight_2

		# Gather list of all files in directory "1", and confirm they
		# are also in directory "2"
		filenames = []
		for i in os.scandir(scenario_input(ve_scenario_dir,'1')):
			if i.is_file():
				filenames.append(i.name)
				f2 = scenario_input(ve_scenario_dir,'2', i.name)
				if not os.path.exists(f2):
					raise FileNotFoundError(f2)

		for filename in filenames:
			df1 = pd.read_csv(scenario_input(ve_scenario_dir,'1',filename))
			df2 = pd.read_csv(scenario_input(ve_scenario_dir,'2',filename))

			float_mix_cols = list(df1.select_dtypes('float').columns)
			for j in no_mix_cols:
				if j in float_mix_cols:
					float_mix_cols.remove(j)

			if float_mix_cols:
				df1_float = df1[float_mix_cols]
				df2_float = df2[float_mix_cols]
				df1[float_mix_cols] = df1_float * weight_1 + df2_float * weight_2

			int_mix_cols = list(df1.select_dtypes('int').columns)
			for j in no_mix_cols:
				if j in int_mix_cols:
					int_mix_cols.remove(j)

			if int_mix_cols:
				df1_int = df1[int_mix_cols]
				df2_int = df2[int_mix_cols]
				df_int_mix = df1_int * weight_1 + df2_int * weight_2
				df1[int_mix_cols] = np.round(df_int_mix).astype(int)

			out_filename = join_norm(
				self.resolved_model_path, 'inputs', filename
			)
			df1.to_csv(out_filename, index=False, float_format="%.5f")


	def run(self):
		"""
		Run the core model.

		This method is the place where the RSPM core model run takes place.
		Note that this method takes no arguments; all the input
		exogenous uncertainties and policy levers are delivered to the
		core model in the `setup` method, which will be executed prior
		to calling this method. This facilitates debugging, as the `setup`
		method can potentially be used without the `run` method, allowing
		the user to manually inspect the prepared files and ensure they
		are correct before actually running a potentially expensive model.
		When running experiments, this method is called once for each core
		model experiment, after the `setup` method completes.

		Raises:
		    UserWarning: If model is not properly setup
		"""
		_logger.info("VEState RUN ...")

		# Set the R path
		os.environ['path'] = join_norm(self.config['r_executable'])+';'+os.environ['path']

		# This demo uses the `Rscript` command line tool to run R
		# programmatically.  On Windows, the tool also includes `.exe`.
		if platform.system() == 'Windows':
			cmd = 'Rscript.exe'
		else:
			cmd = 'Rscript'

		# Remove existing datastore
		# datastore_namer = re.compile(r"Datastore_202[0-9]-[0-9]+-[0-9]+_[0-9]")
		datastore_namer = re.compile(r".*Datastore")
		for outfolder in glob.glob(join_norm(self.local_directory, self.model_path, '*/')):
			_logger.debug(f"VEState RUN removing: {outfolder}")
			if datastore_namer.match(outfolder):
				_logger.debug('VEState Removing existing Datastore ' + outfolder + ' ...')
				remove_tree(
					join_norm(outfolder),
					verbose=0
				)

		# Copy the common datastore that contains information of historical runs including
		# baseyear run
		_logger.info('VEState Copying the historical datastore ...')
		copy_tree(
			self._hist_datastore_dir,
			join_norm(self.local_directory, self.model_path),
		)
		# shutil.copyfile(
		# 	join_norm(this_directory, 'ModelState.Rda'),
		# 	join_norm(self.local_directory, self.model_path, 'ModelState.Rda'),
		# )
		_logger.info('VEState Finished copying the historical datastore ...')


		# Write a small script that will run the model under VisionEval 2.0
		with open(join_norm(self.local_directory, "vestate_runner.R"), "wt") as r_script:
			r_script.write(f"""
			require(visioneval)
			source("{r_join_norm(self.config['r_runtime_path'], 'VisionEval.R')}", chdir = TRUE)
			source("{r_join_norm(self.local_directory, self.model_path, 'run_model.R')}", echo=TRUE, chdir = TRUE)
			#cwd <- setwd("{r_join_norm(self.local_directory, self.model_path)}")
			#on.exit(setwd(cwd))
			#source("run_model.R", echo=TRUE)
			#thismodel <- openModel("{r_join_norm(self.local_directory, self.model_path)}")
			#thismodel$run()
			#thismodel$extract()
			#thismodel$query(Geography=c(Type='Marea',Value='RVMPO'))
			#Add source script
			""")

		# Ensure that R paths are set correctly.
		r_lib = self.config['r_library_path']
		with open(join_norm(self.local_directory, '.Rprofile'), 'wt') as rprof:
			rprof.write(f'.libPaths("{r_lib}")\n')

		# The subprocess.run command runs a command line tool. The
		# name of the command line tool, plus all the command line arguments
		# for the tool, are given as a list of strings, not one string.
		# The `cwd` argument sets the current working directory from which the
		# command line tool is launched.  Setting `capture_output` to True
		# will capture both stdout and stderr from the command line tool, and
		# make these available in the result to facilitate debugging.
		self.last_run_result = subprocess.run(
			[cmd, 'vestate_runner.R'],
			cwd=self.local_directory,
			capture_output=True,
		)
		if self.last_run_result.returncode:
			raise subprocess.CalledProcessError(
				self.last_run_result.returncode,
				self.last_run_result.args,
				self.last_run_result.stdout,
				self.last_run_result.stderr,
			)
		else:
			if not os.path.exists(join_norm(self.local_directory, self.model_path, 'output')):
				os.mkdir(join_norm(self.local_directory, self.model_path, 'output'))
			with open(join_norm(self.local_directory, self.model_path, 'output', 'stdout.log'), 'wb') as slog:
				slog.write(self.last_run_result.stdout)

		# VisionEval Version 2 appends timestamps to output filenames,
		# but because we're running in a temporary directory, we can
		# strip them down to standard filenames.
		# import re, glob
		# renamer = re.compile(r"(.*)_202[0-9]-[0-9]+-[0-9]+_[0-9]+(\.csv)")
		# _logger.debug("VEState RUN renaming files")
		# for outfile in glob.glob(join_norm(self.local_directory, self.model_path, 'output', '*.csv')):
		# 	_logger.debug(f"VEState RUN renaming: {outfile}")
		# 	if renamer.match(outfile):
		# 		newname = renamer.sub(r"\1\2", outfile)
		# 		_logger.debug(f"     to: {newname}")
		# 		if os.path.exists(newname):
		# 			os.remove(newname)
		# 		os.rename(outfile, newname)

		_logger.info("VEState RUN complete")

	def last_run_logs(self, output=None):
		"""
		Display the logs from the last run.
		"""
		if output is None:
			output = print
		def to_out(x):
			if isinstance(x, bytes):
				output(x.decode())
			else:
				output(x)
		try:
			last_run_result = self.last_run_result
		except AttributeError:
			output("no run stored")
		else:
			if last_run_result.stdout:
				output("=== STDOUT ===")
				to_out(last_run_result.stdout)
			if last_run_result.stderr:
				output("=== STDERR ===")
				to_out(last_run_result.stderr)
			output("=== END OF LOG ===")


	# def post_process(self, params=None, measure_names=None, output_path=None):
	# 	"""
	# 	Runs post processors associated with particular performance measures.

	# 	This method is the place to conduct automatic post-processing
	# 	of core model run results, in particular any post-processing that
	# 	is expensive or that will write new output files into the core model's
	# 	output directory.  The core model run should already have
	# 	been completed using `setup` and `run`.  If the relevant performance
	# 	measures do not require any post-processing to create (i.e. they
	# 	can all be read directly from output files created during the core
	# 	model run itself) then this method does not need to be overloaded
	# 	for a particular core model implementation.

	# 	Args:
	# 		params (dict):
	# 			Dictionary of experiment variables, with keys as variable names
	# 			and values as the experiment settings. Most post-processing
	# 			scripts will not need to know the particular values of the
	# 			inputs (exogenous uncertainties and policy levers), but this
	# 			method receives the experiment input parameters as an argument
	# 			in case one or more of these parameter values needs to be known
	# 			in order to complete the post-processing.  In this demo, the
	# 			params are not needed, and the argument is optional.
	# 		measure_names (List[str]):
	# 			List of measures to be processed.  Normally for the first pass
	# 			of core model run experiments, post-processing will be completed
	# 			for all performance measures.  However, it is possible to use
	# 			this argument to give only a subset of performance measures to
	# 			post-process, which may be desirable if the post-processing
	# 			of some performance measures is expensive.  Additionally, this
	# 			method may also be called on archived model results, allowing
	# 			it to run to generate only a subset of (probably new) performance
	# 			measures based on these archived runs. In this demo, the
	# 			the argument is optional; if not given, all measures will be
	# 			post-processed.
	# 		output_path (str, optional):
	# 			Path to model outputs.  If this is not given (typical for the
	# 			initial run of core model experiments) then the local/default
	# 			model directory is used.  This argument is provided primarily
	# 			to facilitate post-processing archived model runs to make new
	# 			performance measures (i.e. measures that were not in-scope when
	# 			the core model was actually run).

	# 	Raises:
	# 		KeyError:
	# 			If post process is not available for specified measure
	# 	"""

	# 	# Derived from VEStateResults.R in VisionEval package, this script
	# 	# generates a few more aggregate outputs.

	# 	if output_path is None:
	# 		output_path = join_norm(self.local_directory, self.model_path, self.rel_output_path)
	# 	marea_2038 = pd.read_csv(
	# 		join_norm(output_path, 'Marea_2038_1.csv'),
	# 	)
	# 	household_2038 = pd.read_csv(
	# 		join_norm(output_path, 'Household_2038_1.csv'),
	# 	)

	# 	population = household_2038['HhSize'].sum()
	# 	GHGReduction = 0
	# 	DVMTPerCapita = household_2038['Dvmt'].sum() / population
	# 	WalkTravelPerCapita = household_2038['WalkTrips'].sum() / population
	# 	AirPollutionEm = household_2038['DailyCO2e'].sum()
	# 	FuelUse = (
	# 		household_2038['DailyGGE'].sum()
	# 		+ marea_2038['ComSvcUrbanGGE'].sum()
	# 		+ marea_2038['ComSvcNonUrbanGGE'].sum()
	# 	) * 365
	# 	TruckDelay = 0
	# 	OperationCost = household_2038['AveVehCostPM'] * household_2038['Dvmt']
	# 	TotalCost = household_2038['OwnCost']+OperationCost
	# 	VehicleCost = TotalCost.sum()/household_2038['Income'].sum() * 100

	# 	def deflateCurrency(values, FromYear, ToYear):
	# 		deflators_df = pd.read_csv(join_norm(self.model_path, 'defs', 'deflators.csv'))
	# 		deflators_df.index = deflators_df['Year'].astype(str)
	# 		FromYear = str(FromYear)
	# 		ToYear = str(ToYear)
	# 		if FromYear not in deflators_df.index:
	# 			raise KeyError(f"invalid FromYear {FromYear}")
	# 		if ToYear not in deflators_df.index:
	# 			raise KeyError(f"invalid ToYear {ToYear}")
	# 		return values * deflators_df.loc[ToYear, 'Value'] / deflators_df.loc[FromYear, 'Value']

	# 	BaseYear = 2010
	# 	Income2005 = deflateCurrency(household_2038['Income'], BaseYear, "2005")
	# 	IsLowIncome = Income2005 < 20000
	# 	VehicleCostLow = TotalCost[IsLowIncome].sum()/household_2038[IsLowIncome]['Income'].sum() * 100

	# 	result = dict(
	# 		GHGReduction=GHGReduction,
	# 		DVMTPerCapita=DVMTPerCapita,
	# 		WalkTravelPerCapita=WalkTravelPerCapita,
	# 		TruckDelay=TruckDelay,
	# 		AirPollutionEm=AirPollutionEm,
	# 		FuelUse=FuelUse,
	# 		VehicleCost=VehicleCost,
	# 		VehicleCostLow=VehicleCostLow,
	# 	)

	# 	with open(join_norm(output_path, 'ComputedMeasures.json'), 'wt') as out:
	# 		json.dump(result, out)


	def archive(self, params, model_results_path=None, experiment_id=None):
		"""
		Copies model outputs to archive location.

		Args:
			params (dict):
				Dictionary of experiment variables
			model_results_path (str, optional):
				The archive path to use.  If not given, a default
				archive path is constructed based on the scope name
				and the experiment_id.
			experiment_id (int, optional):
				The id number for this experiment.  Ignored if the
				`model_results_path` argument is given.

		"""
		if model_results_path is None:
			if experiment_id is None:
				db = getattr(self, 'db', None)
				if db is not None:
					experiment_id = db.get_experiment_id(self.scope.name, None, params)
			model_results_path = self.get_experiment_archive_path(experiment_id)
		zipname = os.path.join(model_results_path, 'run_archive')
		_logger.info(
			f"VEState ARCHIVE\n"
			f" from: {join_norm(self.local_directory, self.model_path, self.rel_output_path)}\n"
			f"   to: {zipname}.zip"
		)
		shutil.make_archive(
			zipname, 'zip',
			root_dir=join_norm(self.local_directory, self.model_path),
			base_dir=self.rel_output_path,
		)

