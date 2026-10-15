local p = {}

local getArgs = require('Module:Arguments').getArgs
local yesno = require('Module:Yesno')


local dataModules = {
	--Production I
    mw.loadData('Module:Recipe/Refining_Unit'),
    mw.loadData('Module:Recipe/Shredding_Unit'),
    mw.loadData('Module:Recipe/Fitting_Unit'),
    mw.loadData('Module:Recipe/Moulding_Unit'),
    mw.loadData('Module:Recipe/Planting_Unit'),
    mw.loadData('Module:Recipe/Seed-Picking_Unit'),
	--Production II / Assembly
    mw.loadData('Module:Recipe/Gearing_Unit'),
    mw.loadData('Module:Recipe/Filling_Unit'),
    mw.loadData('Module:Recipe/Packaging_Unit'),
    mw.loadData('Module:Recipe/Grinding_Unit'),
    mw.loadData('Module:Recipe/Reactor_Crucible'),
    mw.loadData('Module:Recipe/Expanded_Crucible'),
    mw.loadData('Module:Recipe/Forge_of_the_Sky'),
    mw.loadData('Module:Recipe/Separating_Unit'),
    mw.loadData('Module:Recipe/Purification_Unit'),
    mw.loadData('Module:Recipe/Fluid-Gas_Transmuting_Unit'),
    mw.loadData('Module:Recipe/Solid-Gas_Transmuting_Unit'),
    mw.loadData('Module:Recipe/Gas_Reactor_Globe'),
    --Events
    mw.loadData('Module:Recipe/Palm-Top_Savior'),
    mw.loadData('Module:Recipe/Chubby_Lung_Attacks'),
	--Manual Crafting
    mw.loadData('Module:Recipe/Crafting'),
    mw.loadData('Module:Recipe/Construct'),
	--Thermal Bank
    mw.loadData('Module:Recipe/Thermal_Bank'),
}

local data = {}
for _, module in ipairs(dataModules) do
    for _, recipe in ipairs(module) do
        table.insert(data, recipe)
    end
end

local itemModule = require('Module:Item')
local currentFrame = mw.getCurrentFrame()

local function renderItemIcon(itemName, count)
    return itemModule.renderItemBox(itemName, count)
end

function p._main(args)
    local facilityFilter = args.facility and mw.ustring.lower(args.facility)
    local productFilter = args.product and mw.ustring.lower(args.product)
    local ingredientFilter = args.ingredient and mw.ustring.lower(args.ingredient)
    local ingredientExclude = args.ingredientExclude and mw.ustring.lower(args.ingredientExclude)
    local modeFilter = args.mode and mw.ustring.lower(args.mode)
    local eventFilter = yesno(args.event, false)
    if eventFilter == nil then
        eventFilter = false
    end
    local eventNameFilter = args.eventName and mw.ustring.lower(args.eventName)
    local environmentFilter = args.environment and mw.ustring.lower(args.environment)
    local categoryFilter = args.category and mw.ustring.lower(args.category)

    local showFacilityColumn = yesno(args.showFacility, true)
    if showFacilityColumn == nil then
        showFacilityColumn = true
    end
    
    local out = {}
    table.insert(out, '{| class="wikitable"')

    local header = '|-\n'
    if showFacilityColumn then
        header = header .. '! Facility\n'
    end
    header = header ..
        '! Cost\n' ..
        '! Product\n' ..
        '! Time'
    table.insert(out, header)

    for _, recipe in ipairs(data) do
        local include = true

		-- check if the recipe is actually something that is asked for, otherwise skip to next recipe
        if facilityFilter and mw.ustring.lower(recipe.facility or '') ~= facilityFilter then
            include = false
        end

        if include and productFilter then
            include = false
            for _, prod in ipairs(recipe.products or {}) do
                if mw.ustring.lower(prod.item or '') == productFilter then
                    include = true
                    break
                end
            end
        end

        if include and ingredientFilter then
            include = false
            for _, ing in ipairs(recipe.ingredients or {}) do
                if mw.ustring.lower(ing.item or '') == ingredientFilter then
                    include = true
                    break
                end
            end
        end
        
        if include and ingredientExclude then
    		for _, ing in ipairs(recipe.ingredients or {}) do
        		if mw.ustring.lower(ing.item or '') == ingredientExclude then
            		include = false
            		break
        		end
    		end
        end
	
		if include and modeFilter and mw.ustring.lower(recipe.mode or '') ~= modeFilter then
		    include = false
		end

		if include and eventFilter ~= nil and (recipe.event or false) ~= eventFilter then
			include = false
		end
		
		if include and eventNameFilter and mw.ustring.lower(recipe.eventName or '') ~= eventNameFilter then
			include = false
		end
		
		if include and environmentFilter and mw.ustring.lower(recipe.enviroment or '') ~= environmentFilter then
			include = false
		end
		
		if include and categoryFilter and mw.ustring.lower(recipe.category or '') ~= categoryFilter then
			include = false
		end

        if include then
			local ingOut = ""
        	local prodOut = ""
            for _, ing in ipairs(recipe.ingredients or {}) do
                if recipe.time then
                	ingOut = ingOut .. "<div style='display: inline-block;'>" .. tostring(renderItemIcon(ing.item, ing.count)) .. "<br>" .. tostring(ing.count / recipe.time * 60 .. "/min") .. "</div> "
                else
                	ingOut = ingOut .. tostring(renderItemIcon(ing.item, ing.count))
                end
            end

            for _, prod in ipairs(recipe.products or {}) do
                if recipe.time then
                	prodOut =  prodOut .. "<div style='display: inline-block;'>" .. tostring(renderItemIcon(prod.item, prod.count)) .. "<br>" .. tostring(prod.count / recipe.time * 60 .. "/min") .. "</div> "
				else
					prodOut =  prodOut .. tostring(renderItemIcon(prod.item, prod.count))
				end
            end
            -- for special recipes with no prodcut such as Treating Unit
            if prodOut == nil then
            	prodOut = '-'
            end
            
            
            local enviromentRow = ""
            
            
            if recipe.enviroment ~= nil and recipe.enviroment ~= "" then
            	local extraRowBackgroundColor = ""
            	local enviromentRowStyling = ""
            	
            	if recipe.enviroment == "Stable ENV" then
            		extraRowBackgroundColor = '#38C0FC80'
            		enviromentRowStyling = 'style="background-color: #38C0FC20"'
            	elseif recipe.enviroment == "Acrid ENV" then
            		extraRowBackgroundColor = '#FFB90180'
    				enviromentRowStyling = 'style="background-color: #FFB90120'
    			end
            	enviromentRow = string.format('| style="background-color: %s;" colspan="100%%" | %s\n|-%s\n',
            		extraRowBackgroundColor,
            		recipe.enviroment,
            		enviromentRowStyling)
            end
            
            local row = '|-\n'
    		row = row .. enviromentRow
            
            if showFacilityColumn then
                local facilityCell
                if recipe.facility == "Crafting" then
                    facilityCell = '<span title="Manual crafting via the crafting menu accessed from the backpack panel">Crafting</span>'
                elseif recipe.facility == "Construct" then
                    facilityCell = '<span title="Manual crafting via the construction menu">Construct</span>'
                else
                    facilityCell = tostring(renderItemIcon(recipe.facility or ' ', nil))
                end
                row = row .. string.format('| style="width: 14%%; padding-top: 10px;" | %s\n', facilityCell)
            end
        
        	local timeText = recipe.time and (recipe.time .. 's') or '-'

            row = row ..
                string.format('| style="width: 42%%; padding-top: 10px;" | %s\n',
                    ingOut
                ) ..
                string.format('| style="width: 30%%; padding-top: 10px;" | %s\n',
                    prodOut
                ) ..
                string.format('| style="width: 14%%; padding-top: 10px;" | %s',
                    timeText
                )
            table.insert(out, row)
        end
    end

    table.insert(out, '|}')
    return table.concat(out, '\n')
end

function p.main(frame)
    local args = getArgs(frame)
    return p._main(args)
end

return p