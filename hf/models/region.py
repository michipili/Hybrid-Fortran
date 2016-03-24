#!/usr/bin/python
# -*- coding: UTF-8 -*-

# Copyright (C) 2016 Michel Müller, Tokyo Institute of Technology

# This file is part of Hybrid Fortran.

# Hybrid Fortran is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Hybrid Fortran is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with Hybrid Fortran. If not, see <http://www.gnu.org/licenses/>.

import weakref, copy
from tools.commons import enum
from tools.metadata import getArguments
from tools.patterns import RegExPatterns
from models.symbol import DeclarationType
from machinery.commons import ConversionOptions, getSymbolAccessStringAndReminder

RegionType = enum(
	"MODULE_DECLARATION",
	"KERNEL_CALLER_DECLARATION",
	"OTHER"
)

class Region(object):
	def __init__(self, routine):
		self._linesAndSymbols = []
		self._routineRef = weakref.ref(routine)
		self._parentRegion = None

	@property
	def parentRoutine(self):
		return self._routineRef()

	@parentRoutine.setter
	def parentRoutine(self, _routine):
		self._routineRef = weakref.ref(_routine)

	def _sanitize(self, text, skipDebugPrint=False):
		if not ConversionOptions.Instance().debugPrint or skipDebugPrint:
			return text.strip() + "\n"
		return "!<--- %s\n%s\n!--->\n" %(
			type(self),
			text.strip()
		)

	def clone(self):
		region = self.__class__(self.parentRoutine)
		region._linesAndSymbols = copy.deepcopy(self._linesAndSymbols)
		return region

	def loadParentRegion(self, region):
		self._parentRegion = weakref.ref(region)

	def loadLine(self, line, symbolsOnCurrentLine=None):
		stripped = line.strip()
		if stripped == "":
			return
		self._linesAndSymbols.append((
			stripped,
			symbolsOnCurrentLine
		))

	def implemented(self, skipDebugPrint=False):
		text = "\n".join([line for (line, symbols) in self._linesAndSymbols])
		if text == "":
			return ""
		return self._sanitize(text, skipDebugPrint)

class CallRegion(Region):
	def __init__(self, routine):
		super(CallRegion, self).__init__(routine)
		self._callee = None
		self._passedInSymbolsByName = None
		self._passedInSymbolsByNameInScope = None

	def _adjustedArguments(self, arguments):
		def adjustArgument(argument, parallelRegionTemplate, iterators):
			symbolMatch = RegExPatterns.Instance().symbolNamePattern.match(argument)
			if not symbolMatch:
				return argument
			symbol = self._passedInSymbolsByNameInScope.get(symbolMatch.group(1))
			if not symbol:
				return argument
			symbolAccessString, remainder = getSymbolAccessStringAndReminder(
				symbol,
				iterators,
				parallelRegionTemplate,
				symbolMatch.group(2),
				self._callee,
				isInsideParallelRegion=parallelRegionTemplate != None
			)
			return symbolAccessString + remainder

		parallelRegionTemplate = None
		if self._parentRegion and isinstance(self._parentRegion(), ParallelRegion):
			parallelRegionTemplate = self._parentRegion().template
		iterators = self._callee.implementation.getIterators(parallelRegionTemplate) if parallelRegionTemplate else []
		return [adjustArgument(argument, parallelRegionTemplate, iterators) for argument in arguments]

	def loadCallee(self, callee):
		self._callee = callee

	def loadPassedInSymbolsByName(self, symbolsByName):
		self._passedInSymbolsByName = copy.copy(symbolsByName)
		self._passedInSymbolsByNameInScope = dict(
			(symbol.nameInScope(), symbol)
			for symbol in symbolsByName.values()
		)

	def clone(self):
		raise NotImplementedError()

	def implemented(self, skipDebugPrint=False):
		if not self._callee:
			raise Exception("call needs to be loaded at this point")

		text = ""
		argumentSymbols = None
		#this hasattr is used to test the callee for analyzability without circular imports
		if hasattr(self._callee, "implementation"):
			#$$$ we could have an ordering problem with _passedInSymbolsByName
			argumentSymbols = self._callee.additionalArgumentSymbols + self._passedInSymbolsByName.values()
			for symbol in argumentSymbols:
				text += self._callee.implementation.callPreparationForPassedSymbol(
					self._routineRef().node,
					symbolInCaller=symbol
				)

		parallelRegionPosition = None
		if hasattr(self._callee, "implementation"):
			parallelRegionPosition = self._callee.node.getAttribute("parallelRegionPosition")
		if hasattr(self._callee, "implementation") and parallelRegionPosition == "within":
			if not self._callee.parallelRegionTemplates \
			or len(self._callee.parallelRegionTemplates) == 0:
				raise Exception("No parallel region templates found for subroutine %s" %(
					self._callee.name
				))
			text += "%s call %s %s" %(
				self._callee.implementation.kernelCallPreparation(
					self._callee.parallelRegionTemplates[0],
					calleeNode=self._callee.node
				),
				self._callee.name,
				self._callee.implementation.kernelCallConfig()
			)
		else:
			text += "call " + self._callee.name

		text += "("
		if hasattr(self._callee, "implementation"):
			if len(self._callee.additionalArgumentSymbols) > 0:
				text += " &\n"
			bridgeStr1 = " & !additional parameter"
			bridgeStr2 = "inserted by framework\n& "
			numOfProgrammerSpecifiedArguments = len(self._callee.programmerArguments)
			for symbolNum, symbol in enumerate(self._callee.additionalArgumentSymbols):
				hostName = symbol.nameInScope()
				text += hostName
				if symbolNum < len(self._callee.additionalArgumentSymbols) - 1 or numOfProgrammerSpecifiedArguments > 0:
					text += ", %s (type %i) %s" %(bridgeStr1, symbol.declarationType, bridgeStr2)
		text += ", ".join(self._adjustedArguments(self._callee.programmerArguments)) + ")\n"

		if hasattr(self._callee, "implementation"):
			allSymbolsPassedByName = dict(
				(symbol.name, symbol)
				for symbol in argumentSymbols
			)
			text += self._callee.implementation.kernelCallPost(
				allSymbolsPassedByName,
				self._callee.node
			)
			for symbol in argumentSymbols:
				text += self._callee.implementation.callPostForPassedSymbol(
					self._routineRef().node,
					symbolInCaller=symbol
				)
		return self._sanitize(text, skipDebugPrint)

class ParallelRegion(Region):
	def __init__(self, routine):
		super(ParallelRegion, self).__init__(routine)
		self._currRegion = Region(routine)
		self._subRegions = [self._currRegion]
		self._activeTemplate = None

	@property
	def template(self):
		return self._activeTemplate

	def switchToRegion(self, region):
		self._currRegion = region
		self._subRegions.append(region)
		region.loadParentRegion(self)

	def loadLine(self, line, symbolsOnCurrentLine=None):
		self._currRegion.loadLine(line, symbolsOnCurrentLine)

	def loadActiveParallelRegionTemplate(self, templateNode):
		self._activeTemplate = templateNode

	def clone(self):
		raise NotImplementedError()

	def implemented(self, skipDebugPrint=False):
		parentRoutine = self._routineRef()
		hasParallelRegionWithin = parentRoutine.node.getAttribute('parallelRegionPosition') == 'within'
		if hasParallelRegionWithin \
		and not self._activeTemplate:
			raise Exception("cannot implement parallel region without a template node loaded")

		text = ""
		if hasParallelRegionWithin:
			text += parentRoutine.implementation.parallelRegionBegin(
				parentRoutine.symbolsByName.values(),
				self._activeTemplate
			).strip() + "\n"
		text += "\n".join([region.implemented() for region in self._subRegions])
		if hasParallelRegionWithin:
			text += parentRoutine.implementation.parallelRegionEnd(
				self._activeTemplate
			).strip() + "\n"
		return self._sanitize(text, skipDebugPrint)

class RoutineSpecificationRegion(Region):
	def __init__(self, routine):
		super(RoutineSpecificationRegion, self).__init__(routine)
		self._additionalParametersByKernelName = None
		self._packedRealSymbolsByCalleeName = None
		self._symbolsToAdd = None
		self._compactionDeclarationPrefixByCalleeName = None
		self._currAdditionalCompactedSubroutineParameters = None

	def loadAdditionalContext(
		self,
		additionalParametersByKernelName,
		packedRealSymbolsByCalleeName,
		symbolsToAdd,
		compactionDeclarationPrefixByCalleeName,
		currAdditionalCompactedSubroutineParameters
	):
		self._additionalParametersByKernelName = additionalParametersByKernelName
		self._packedRealSymbolsByCalleeName = packedRealSymbolsByCalleeName
		self._symbolsToAdd = symbolsToAdd
		self._compactionDeclarationPrefixByCalleeName = compactionDeclarationPrefixByCalleeName
		self._currAdditionalCompactedSubroutineParameters = currAdditionalCompactedSubroutineParameters

	def implemented(self, skipDebugPrint=False):
		parentRoutine = self._routineRef()

		text = super(RoutineSpecificationRegion, self).implemented(skipDebugPrint=True)

		numberOfAdditionalDeclarations = (
			len(sum([
				self._additionalParametersByKernelName[kname][1]
				for kname in self._additionalParametersByKernelName
			], [])) + len(self._symbolsToAdd) + len(self._packedRealSymbolsByCalleeName.keys())
		)
		if numberOfAdditionalDeclarations > 0:
			text += "\n! ****** additional symbols inserted by framework to emulate device support of language features\n"
		declarationRegionType = RegionType.OTHER
		if parentRoutine.isCallingKernel:
			declarationRegionType = RegionType.KERNEL_CALLER_DECLARATION
		defaultPurgeList = ['intent', 'public', 'parameter', 'allocatable']
		for symbol in self._symbolsToAdd:
			purgeList = defaultPurgeList
			if not symbol.isCompacted:
				purgeList=['public', 'parameter', 'allocatable']
			text += parentRoutine.implementation.adjustDeclarationForDevice(
				symbol.getDeclarationLineForAutomaticSymbol(purgeList).strip(),
				[symbol],
				declarationRegionType,
				parentRoutine.node.getAttribute('parallelRegionPosition')
			).rstrip() + " ! type %i symbol added for this subroutine\n" %(symbol.declarationType)
		for calleeName in self._additionalParametersByKernelName:
			additionalImports, additionalDeclarations = self._additionalParametersByKernelName[calleeName]
			additionalImportSymbolsByName = {}
			for symbol in additionalImports:
				additionalImportSymbolsByName[symbol.name] = symbol

			callee = parentRoutine.callsByCalleeName.get(calleeName)
			if not callee:
				raise Exception("kernel %s is not loaded properly in routine %s" %(calleeName, parentRoutine.name))
			implementation = callee.implementation
			for symbol in parentRoutine.filterOutSymbolsAlreadyAliveInCurrentScope(additionalDeclarations):
				if symbol.declarationType not in [DeclarationType.LOCAL_ARRAY, DeclarationType.LOCAL_SCALAR]:
					# only symbols that are local to the kernel actually need to be declared here.
					# Everything else we should have in our own scope already, either through additional imports or
					# through module association (we assume the kernel and its wrapper reside in the same module)
					continue

				#in case the array uses domain sizes in the declaration that are additional symbols themselves
				#we need to fix them.
				adjustedDomains = []
				for (domName, domSize) in symbol.domains:
					domSizeSymbol = additionalImportSymbolsByName.get(domSize)
					if domSizeSymbol is None:
						adjustedDomains.append((domName, domSize))
						continue
					adjustedDomains.append((domName, domSizeSymbol.nameInScope()))
				symbol.domains = adjustedDomains

				text += implementation.adjustDeclarationForDevice(
					symbol.getDeclarationLineForAutomaticSymbol(defaultPurgeList).strip(),
					[symbol],
					declarationRegionType,
					parentRoutine.node.getAttribute('parallelRegionPosition')
				).rstrip() + " ! type %i symbol added for callee %s\n" %(symbol.declarationType, calleeName)
			toBeCompacted = self._packedRealSymbolsByCalleeName.get(calleeName, [])
			if len(toBeCompacted) > 0:
				#TODO: generalize for cases where we don't want this to be on the device (e.g. put this into Implementation class)
				compactedArrayName = "hfimp_%s" %(calleeName)
				compactedArray = FrameworkArray(
					compactedArrayName,
					self._compactionDeclarationPrefixByCalleeName[calleeName],
					domains=[("hfauto", str(len(toBeCompacted)))],
					isOnDevice=True
				)
				text += implementation.adjustDeclarationForDevice(
					compactedArray.getDeclarationLineForAutomaticSymbol().strip(),
					[compactedArray],
					declarationRegionType,
					parentRoutine.node.getAttribute('parallelRegionPosition')
				).rstrip() + " ! compaction array added for callee %s\n" %(calleeName)

		calleesWithPackedReals = self._packedRealSymbolsByCalleeName.keys()
		for calleeName in calleesWithPackedReals:
			for idx, symbol in enumerate(sorted(self._packedRealSymbolsByCalleeName[calleeName])):
				#$$$ clean this up, the hf_imp prefix should be decided within the symbol class
				text += "hfimp_%s(%i) = %s" %(calleeName, idx+1, symbol.nameInScope()) + \
					 " ! type %i symbol compaction for callee %s\n" %(symbol.declarationType, calleeName)

		for idx, symbol in enumerate(self._currAdditionalCompactedSubroutineParameters):
			#$$$ clean this up, the hf_imp prefix should be decided within the symbol class
			text += "%s = hfimp_%s(%i)" %(symbol.nameInScope(), parentRoutine.name, idx+1) + \
					 " ! additional type %i symbol compaction\n" %(symbol.declarationType)

		if numberOfAdditionalDeclarations > 0:
			text += "! ****** end additional symbols\n\n"

		text += parentRoutine.implementation.declarationEnd(
			parentRoutine.symbolsByName.values() + parentRoutine.additionalImports,
			parentRoutine.isCallingKernel,
			parentRoutine.node,
			parentRoutine.parallelRegionTemplates
		)
		return self._sanitize(text, skipDebugPrint)

class RoutineEarlyExitRegion(Region):
	def implemented(self, skipDebugPrint=False):
		parentRoutine = self._routineRef()
		text = parentRoutine.implementation.subroutineExitPoint(
			parentRoutine.symbolsByName.values(),
			parentRoutine.isCallingKernel,
			isSubroutineEnd=False
		)
		text += super(RoutineExitRegion, self).implemented(skipDebugPrint=True)

		return self._sanitize(text, skipDebugPrint)